#!/usr/bin/env python3
"""Lightweight async load harness for GeoID (no k6/Locust install needed).

    # the stack must be running
    uv run python scripts/loadtest.py --scenario all --concurrency 16 --duration 10
    uv run python scripts/loadtest.py --scenario mint  --concurrency 32 --duration 20

Scenarios
    mint  — POST a unique polygon each time   (single-POST hot path; expects 201)
    dedup — POST the SAME polygon every time   (idempotent re-mint/arbiter hot
                                                path; every request expects 201)

NOTE: a local PostGIS running under amd64 emulation on Apple Silicon is several
times slower than native — treat these numbers as a pessimistic LOWER BOUND and
re-run on the target infra (Cloud Run + Cloud SQL) for representative figures.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import random
import statistics
import time

import httpx

BASE = os.environ.get("GEOID_BASE_URL", "http://localhost:8000").rstrip("/")
COLLECTION = os.environ.get("GEOID_COLLECTION", "public")


def _polygon(x: float, y: float, s: float = 0.0008) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]],
        },
        "properties": {},
    }


_DEDUP_BODY = _polygon(123.456789, 12.345678)  # fixed geometry → exercises dedup lookup


async def _mint(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"{BASE}/collections/{COLLECTION}/items",
        json=_polygon(random.uniform(-179, 179), random.uniform(-85, 85)),
    )


async def _dedup(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{BASE}/collections/{COLLECTION}/items", json=_DEDUP_BODY)


# (request fn, success statuses): dedup measures the idempotent re-mint/arbiter
# path. A duplicate signal is retired; every successful request is 201.
SCENARIOS = {
    "mint": (_mint, {201}),
    "dedup": (_dedup, {201}),
}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((p / 100) * len(ordered)) - 1)))
    return ordered[idx]


async def _worker(client, fn, ok_statuses, deadline, lat, counters) -> None:
    while time.perf_counter() < deadline:
        start = time.perf_counter()
        try:
            resp = await fn(client)
            lat.append((time.perf_counter() - start) * 1000)
            counters["ok" if resp.status_code in ok_statuses else "err"] += 1
        except Exception:
            counters["err"] += 1


async def run_scenario(name: str, concurrency: int, duration: float) -> dict:
    fn, ok_statuses = SCENARIOS[name]
    lat: list[float] = []
    counters = {"ok": 0, "err": 0}
    limits = httpx.Limits(
        max_connections=concurrency + 16, max_keepalive_connections=concurrency + 16
    )
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        with contextlib.suppress(Exception):
            await fn(client)  # warmup (seeds the dedup incumbent)
        start = time.perf_counter()
        deadline = start + duration
        await asyncio.gather(
            *[
                asyncio.create_task(_worker(client, fn, ok_statuses, deadline, lat, counters))
                for _ in range(concurrency)
            ]
        )
        elapsed = time.perf_counter() - start

    total = len(lat)
    rps = total / elapsed if elapsed else 0.0
    print(
        f"\n[{name}] concurrency={concurrency} duration={duration:.0f}s  requests={total}  ok={counters['ok']} err={counters['err']}  RPS={rps:.1f}"
    )
    if lat:
        print(
            f"  latency ms:  p50={_pct(lat, 50):.1f}  p90={_pct(lat, 90):.1f}  "
            f"p95={_pct(lat, 95):.1f}  p99={_pct(lat, 99):.1f}  max={max(lat):.1f}  mean={statistics.mean(lat):.1f}"
        )
    return {
        "scenario": name,
        "requests": total,
        "rps": rps,
        "p95_ms": _pct(lat, 95),
        "errors": counters["err"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="GeoID async load harness")
    parser.add_argument("--scenario", default="all", choices=[*SCENARIOS, "all"])
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            (await client.get(f"{BASE}/conformance")).raise_for_status()
        except Exception as exc:
            print(f"cannot reach GeoID at {BASE} ({exc}); start the stack first")
            return 1

    print(f"GeoID load test → {BASE} (collection={COLLECTION})")
    print("NOTE: local PostGIS may be amd64-emulated → numbers are a pessimistic lower bound.")
    # mint first (populates the DB), then dedup
    order = ["mint", "dedup"] if args.scenario == "all" else [args.scenario]
    for name in order:
        await run_scenario(name, args.concurrency, args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

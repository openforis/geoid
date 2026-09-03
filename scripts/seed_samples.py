#!/usr/bin/env python3
"""Seed GeoID with the dummy sample plots and demonstrate the core behaviours.

    # stack must be running (docker compose up, or `uv run uvicorn geoid.main:app`)
    uv run python scripts/seed_samples.py

Env:
    GEOID_BASE_URL    default http://localhost:8000
    GEOID_COLLECTION  default public  (anonymous-writable; no token needed)
    GEOID_BEARER_TOKEN a Keycloak JWT; required only when seeding a managed (non-anon) collection

Exit codes: 0 all demos behaved · 1 unreachable or any unexpected response
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from _timing import print_timings, record

BASE = os.environ.get("GEOID_BASE_URL", "http://localhost:8000").rstrip("/")
COLLECTION = os.environ.get("GEOID_COLLECTION", "public")
BEARER_TOKEN = os.environ.get("GEOID_BEARER_TOKEN")
SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BEARER_TOKEN}"} if BEARER_TOKEN else {}


def _post(client: httpx.Client, feature: dict) -> httpx.Response:
    return record(
        "POST items",
        client.post(f"{BASE}/collections/{COLLECTION}/items", json=feature, headers=_headers()),
    )


def main() -> int:
    errors = 0
    with httpx.Client(timeout=30.0) as client:
        try:
            record(
                "GET /conformance (reachability)", client.get(f"{BASE}/conformance")
            ).raise_for_status()
        except Exception as exc:
            print(f"✗ cannot reach GeoID at {BASE} ({exc}).")
            print(
                "  Start it first:  docker compose up --build   (or uv run uvicorn geoid.main:app)"
            )
            return 1
        print(f"→ GeoID at {BASE}, collection '{COLLECTION}'\n")

        first_geoid = None
        print("== Minting sample plots ==")
        feature_collection = json.loads((SAMPLES / "plots.geojson").read_text())
        for feature in feature_collection["features"]:
            resp = _post(client, feature)
            body = resp.json()
            ext = str(feature.get("id"))
            if resp.status_code == 201:
                # Re-running the seed answers 201 again with the SAME geoid — the
                # mint is idempotent, so seeding is safe to repeat.
                print(f"  [201] {ext:<22} {'minted':<18} geoid={body['geoid']}")
            else:
                print(f"  [{resp.status_code}] {ext:<22} ERROR {body}")
                errors += 1
                continue
            if first_geoid is None:
                first_geoid = body["geoid"]

        print("\n== Dedup demo (GH-COCOA-001's geometry, reversed winding, no id) ==")
        dup = json.loads((SAMPLES / "duplicate_of_GH-COCOA-001.geojson").read_text())
        resp = _post(client, dup)
        body = resp.json()
        if resp.status_code == 201 and body.get("geoid") == first_geoid:
            print(f"  [201] no new row; the existing geoid comes back: {body['geoid']}")
        else:
            errors += 1
            print(f"  [{resp.status_code}] expected 201 carrying geoid={first_geoid}, got {body}")

        print("\n== Validation demo (self-intersecting bow-tie) ==")
        invalid = json.loads((SAMPLES / "invalid_selfintersecting.geojson").read_text())
        resp = _post(client, invalid)
        if resp.status_code != 422:
            errors += 1
        print(f"  [{resp.status_code}] {resp.json().get('reason')}")

        if first_geoid:
            # Resolver lives at the app root (BASE already includes any /geoid root_path).
            # Full metadata is member-only; an anonymous seeder gets the masked
            # geometry-only body ({geoid, uri} properties), so print defensively.
            props = record("GET resolve", client.get(f"{BASE}/{first_geoid}")).json()["properties"]
            print(f"\n== Resolve {first_geoid} ==")
            print(f"  uri:         {props['uri']}")
            if "external_id" in props:
                prov = props["_geoid_provenance"]
                print(f"  external_id: {props['external_id']}")
                print(f"  provenance:  {prov['schema']} @ {prov['originating_instance']}")
            else:
                print("  (metadata masked — full features are member/sysadmin-only)")

        print_timings()
        if errors:
            print(f"\n✗ done with {errors} unexpected response(s) — see ERROR/?? lines above")
        else:
            print(f"\n✓ done — explore at {BASE}/docs")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

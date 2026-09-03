"""Shared response-time capture for the dev/ops scripts (smoke_test, seed_samples).

httpx sets ``resp.elapsed`` (send → response-fully-read round trip); just collect it.
Importable because the scripts run as ``python scripts/<name>.py`` (sys.path[0] is
``scripts/``).
"""

from __future__ import annotations

import httpx

RESPONSE_TIMES: list[tuple[str, float]] = []


def record(label: str, resp: httpx.Response) -> httpx.Response:
    RESPONSE_TIMES.append((label, resp.elapsed.total_seconds()))
    return resp


def print_timings() -> None:
    if not RESPONSE_TIMES:
        return
    times = [t for _, t in RESPONSE_TIMES]
    n = len(times)
    print("\nResponse times (ms):")
    for label, t in RESPONSE_TIMES:
        print(f"  {t * 1000:7.1f}  {label}")
    print(
        f"  n={n}  avg={sum(times) / n * 1000:.1f}  "
        f"min={min(times) * 1000:.1f}  max={max(times) * 1000:.1f}"
    )

"""Bulk-path timing gate for the v2 identity recipe (ADR-007 risk: the v2 hash is
plpgsql — never inlined, loops per vertex — and the schema layer adds a per-feature
Python degeneracy pre-check; both run up to GEOID_BULK_MAX_FEATURES times per
synchronous bulk request, 4000 at the deployed setting).

This is a smoke GATE, not a benchmark: the ceiling is deliberately generous (the
deployed request window is 600s on a faster machine than an emulated CI container)
so it only trips on a pathological regression — e.g. the hash accidentally becoming
quadratic — never on ordinary machine noise. The measured duration is printed for
the deploy-time record.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from geoid.config import Settings, get_settings

pytestmark = pytest.mark.integration

_DEPLOYED_BULK_MAX = 4000  # deploy.yml sets GEOID_BULK_MAX_FEATURES=4000
_CEILING_SECONDS = 180.0  # generous vs the deployed 600s window; prints actual


def _distinct_square(i: int) -> dict:
    # Each square in its own lattice cells: 0.001 deg spacing >> the 1e-7 lattice.
    x = -170.0 + (i % 3000) * 0.001
    y = -80.0 + (i // 3000) * 0.001
    side = 0.0005
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + side, y], [x + side, y + side], [x, y + side], [x, y]]],
        },
        "properties": {},
    }


@pytest.fixture
async def deployed_scale_client(db_clean):
    """A client whose app allows the DEPLOYED bulk cap (code default is 1000)."""
    from geoid.main import create_app

    settings = Settings(_env_file=None, bulk_max_features=_DEPLOYED_BULK_MAX)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", timeout=_CEILING_SECONDS
    ) as http_client:
        yield http_client


async def test_max_size_bulk_request_fits_the_request_window(deployed_scale_client):
    body = {
        "type": "FeatureCollection",
        "features": [_distinct_square(i) for i in range(_DEPLOYED_BULK_MAX)],
    }

    started = time.perf_counter()
    resp = await deployed_scale_client.post("/collections/public/items/bulk", json=body)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary == {
        "received": _DEPLOYED_BULK_MAX,
        "accepted": _DEPLOYED_BULK_MAX,
        "rejected": 0,
    }
    print(f"\nbulk timing gate: {_DEPLOYED_BULK_MAX} features in {elapsed:.1f}s")
    assert elapsed < _CEILING_SECONDS, (
        f"max-size bulk took {elapsed:.1f}s (> {_CEILING_SECONDS}s gate) — the v2 "
        "hash path may have regressed pathologically; profile before deploying"
    )

"""Stress/correctness: dedup must stay race-free under concurrent identical POSTs."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_concurrent_identical_posts_converge_to_one_geoid(client, session, unit_square_ccw):
    """K simultaneous POSTs of the SAME geometry → one row, K identical 201s.

    Exercises the arbiter CTE (registry ON CONFLICT DO NOTHING + incumbent-lookup)
    race: one arbiter insert wins and writes the place row; the rest see the
    conflict swallowed and return that winner's geoid. Since the mint is
    idempotent every caller gets the same 201 body — a stronger identity assertion
    than the old one-201-plus-losers split. The invariant is one place row total.
    """
    k = 16
    responses = await asyncio.gather(
        *[client.post("/collections/public/items", json=unit_square_ccw) for _ in range(k)]
    )

    # Assert statuses BEFORE reading geoids so a stray 500 surfaces with its body,
    # not as an opaque KeyError on a missing "geoid" key.
    offenders = [(r.status_code, r.json()) for r in responses if r.status_code != 201]
    assert not offenders, f"unexpected statuses: {offenders}"

    bodies = [r.json() for r in responses]
    assert all(body == bodies[0] for body in bodies), f"bodies diverged: {bodies}"
    assert bodies[0]["geoid"]

    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    registry_count = (
        await session.execute(text("SELECT count(*) FROM geoid_registry"))
    ).scalar_one()
    changelog_count = (await session.execute(text("SELECT count(*) FROM change_log"))).scalar_one()
    assert place_count == 1
    assert registry_count == 1
    assert changelog_count == 1


async def test_concurrent_distinct_posts_all_mint(client, session):
    """K simultaneous POSTs of DISTINCT geometries → K distinct geoids, K rows."""
    k = 16

    def square(i: int) -> dict:
        x = -150 + i  # distinct, well-separated
        return {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[x, 0], [x + 0.5, 0], [x + 0.5, 0.5], [x, 0.5], [x, 0]]],
            },
            "properties": {},
        }

    responses = await asyncio.gather(
        *[client.post("/collections/public/items", json=square(i)) for i in range(k)]
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["geoid"] for r in responses}) == k
    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    assert place_count == k


async def test_resolve_incumbent_returns_committed_collection(client, session, unit_square_ccw):
    """The incumbent-recovery helper reads back the incumbent's slug once visible.

    Covers ``_resolve_incumbent``'s success path deterministically (no race): mint
    a place via the API (committed), then call the helper directly — it must return
    the incumbent's collection slug from the now-visible registry row. The
    concurrent path only reaches this helper when the in-statement LEFT JOIN
    missed; without it that window would raise spurious 500s from the drift guard.
    """
    from geoid.repositories.place_repo import _resolve_incumbent
    from geoid.schemas.place import PlaceCreate, geometry_to_geojson

    assert (await client.post("/collections/public/items", json=unit_square_ccw)).status_code == 201

    geojson = geometry_to_geojson(PlaceCreate.model_validate(unit_square_ccw))
    assert await _resolve_incumbent(session, geojson) == "public"

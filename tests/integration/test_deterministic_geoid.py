"""Integration tests for the deterministic, content-addressed geoid (UUIDv8).

The geoid is derived DB-side from the geometry's canonical ``geom_hash`` (migration
0004), so identity is a pure function of the geometry: same geometry -> same geoid on
every deployment, and the geoid always matches the stored hash it was derived from.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from geoid.domain import geometry_identity, identifiers

pytestmark = pytest.mark.integration


def _polygon_feature(coords: list[list[list[float]]]) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {},
    }


def _feature(coords, geom_type: str) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": {},
    }


# Pinned, cross-deployment geoids under recipe v2 (engine-independent; ADR-007),
# derived from the golden-vector digests (scripts/data/dedup_golden_vectors_v2.json:
# point_origin / multipoint_canonical / baseline_unit_square) via the Python
# reference — kept as literals so drift in EITHER implementation surfaces here.
PINNED_SQUARE_GEOID = "169dc6c3-af8c-80d4-a0b3-436d1bbbde86"
PINNED_POINT_GEOID = "ef8921bd-7767-84df-9977-cc35fcce4be6"
PINNED_MULTIPOINT_GEOID = "f387ff7f-08b6-8d12-9856-17ee8e8cbd53"


async def test_minted_geoid_is_version_8_and_derived_from_stored_hash(
    client, session, unit_square_ccw
):
    # The geoid is the UUIDv8 derivation of the stored geom_hash — identity and
    # dedup can never disagree — and under v2 the pure-Python reference predicts
    # the DB-minted value end-to-end from the request geometry alone.
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]
    assert uuid.UUID(geoid).version == 8

    row = (
        await session.execute(
            text("SELECT geom_hash FROM geoid_registry WHERE geoid = :g"), {"g": geoid}
        )
    ).first()
    assert row is not None
    assert identifiers.geoid_from_geom_hash(bytes(row[0])) == uuid.UUID(geoid)
    assert geometry_identity.geoid_v2(unit_square_ccw["geometry"]) == uuid.UUID(geoid)


async def test_baseline_unit_square_mints_the_pinned_geoid(client):
    # Cross-deployment determinism: POLYGON((0 0,1 0,1 1,0 1,0 0)) is the baseline
    # golden vector (sha256 169dc6c3..., recipe v2). Its geoid is fixed on ANY
    # instance — engine-independent — so this exact value must come back here and
    # on Cloud SQL alike.
    feature = _polygon_feature([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]])
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    assert resp.json()["geoid"] == PINNED_SQUARE_GEOID


async def test_baseline_point_mints_the_pinned_geoid(client):
    # Points go through the same v2 recipe (ADR-005 types), so POINT(0 0) mints a
    # fixed, cross-deployment geoid just like the unit square does.
    resp = await client.post("/collections/public/items", json=_feature([0, 0], "Point"))
    assert resp.status_code == 201
    assert resp.json()["geoid"] == PINNED_POINT_GEOID


async def test_baseline_multipoint_mints_the_pinned_geoid(client):
    resp = await client.post(
        "/collections/public/items", json=_feature([[0, 0], [5, 5]], "MultiPoint")
    )
    assert resp.status_code == 201
    assert resp.json()["geoid"] == PINNED_MULTIPOINT_GEOID


async def test_winding_independent_input_mints_the_same_pinned_geoid(client):
    # The recipe normalizes winding/ring-start, so a CW unit square mints the SAME
    # deterministic geoid as the CCW one above (and 409s if both are posted).
    feature = _polygon_feature([[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]])
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    assert resp.json()["geoid"] == PINNED_SQUARE_GEOID


async def test_distinct_geometries_get_independently_derived_geoids(
    client, session, unit_square_ccw, other_square
):
    a = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    b = (await client.post("/collections/public/items", json=other_square)).json()["geoid"]
    assert a != b
    for geoid in (a, b):
        row = (
            await session.execute(
                text("SELECT geom_hash FROM geoid_registry WHERE geoid = :g"), {"g": geoid}
            )
        ).first()
        assert identifiers.geoid_from_geom_hash(bytes(row[0])) == uuid.UUID(geoid)

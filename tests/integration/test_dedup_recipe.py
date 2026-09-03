"""The dedup-recipe permutation suite — the one place dedup can silently fail.

Tests the authoritative ``geoid_geom_hash_default`` SQL wrapper directly (the
one entry point the arbiter insert and the incumbent-lookup use; since migration
0008 it is the v2 integer-lattice recipe), across the canonicalization
permutations: ring-start rotation, winding direction, hole order, part order,
and sub-grid float jitter must all collapse to the SAME hash; genuinely
different geometry must NOT.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _hash(session, wkt: str) -> str:
    # The v1 geoid_geom_hash(g, grid) is dropped (0008): the lattice scale is a
    # frozen recipe constant, not a parameter — the wrapper IS the recipe.
    stmt = text("SELECT encode(geoid_geom_hash_default(ST_GeomFromText(:wkt, 4326)), 'hex')")
    return (await session.execute(stmt, {"wkt": wkt})).scalar_one()


async def test_ring_start_rotation_yields_same_hash(session):
    base = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    rotated = await _hash(session, "POLYGON((1 1,0 1,0 0,1 0,1 1))")
    assert base == rotated


async def test_winding_direction_yields_same_hash(session):
    ccw = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    cw = await _hash(session, "POLYGON((0 0,0 1,1 1,1 0,0 0))")
    assert ccw == cw


async def test_subgrid_float_jitter_yields_same_hash(session):
    base = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    jittered = await _hash(session, "POLYGON((0.00000001 0,1 0,1 1,0 1,0.00000001 0))")
    assert base == jittered


async def test_hole_order_yields_same_hash(session):
    a = await _hash(
        session,
        "POLYGON((0 0,10 0,10 10,0 10,0 0),(2 2,3 2,3 3,2 3,2 2),(6 6,7 6,7 7,6 7,6 6))",
    )
    b = await _hash(
        session,
        "POLYGON((0 0,10 0,10 10,0 10,0 0),(6 6,7 6,7 7,6 7,6 6),(2 2,3 2,3 3,2 3,2 2))",
    )
    assert a == b


async def test_multipolygon_part_order_yields_same_hash(session):
    a = await _hash(
        session,
        "MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)),((5 5,6 5,6 6,5 6,5 5)))",
    )
    b = await _hash(
        session,
        "MULTIPOLYGON(((5 5,6 5,6 6,5 6,5 5)),((0 0,1 0,1 1,0 1,0 0)))",
    )
    assert a == b


async def test_genuinely_different_geometry_yields_different_hash(session):
    one = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    two = await _hash(session, "POLYGON((0 0,2 0,2 2,0 2,0 0))")
    assert one != two


async def test_supragrid_shift_changes_hash(session):
    # A shift well above the 1e-7 grid is a different place -> different hash.
    base = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    shifted = await _hash(session, "POLYGON((0.001 0,1 0,1 1,0 1,0.001 0))")
    assert base != shifted


async def test_hash_is_sha256_32_bytes(session):
    digest = await _hash(session, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
    assert len(digest) == 64  # 32 bytes hex-encoded


async def test_dedup_is_global_across_collections(client, admin_headers, unit_square_ccw):
    # The SAME geometry in two different collections → the SAME geoid: one
    # geometry → one geoid across the whole catalog, and the second POST is
    # answered exactly like the first (no cross-collection signal at all).
    await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "cola", "public_write": True},
    )
    a = await client.post("/collections/public/items", json=unit_square_ccw)
    b = await client.post("/collections/cola/items", json=unit_square_ccw)
    assert a.status_code == 201
    assert b.status_code == 201
    assert b.json() == a.json()

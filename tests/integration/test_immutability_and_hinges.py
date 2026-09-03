"""Integration tests for immutability (DB trigger) and the two federation hinges."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


async def _mint(client, unit_square_ccw) -> str:
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    return resp.json()["geoid"]


async def test_update_on_place_is_blocked_by_trigger(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(
            text("UPDATE place SET external_id = 'hacked' WHERE id = :id"), {"id": geoid}
        )
        await session.flush()


async def test_delete_on_place_is_blocked_by_trigger(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM place WHERE id = :id"), {"id": geoid})
        await session.flush()


async def test_predecessor_id_is_fully_dropped(client, session, unit_square_ccw):
    # Migration 0013: the supersession column is gone and the immutability
    # message no longer references it.
    column_count = (
        await session.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'place' AND column_name = 'predecessor_id'"
            )
        )
    ).scalar_one()
    assert column_count == 0

    geoid = await _mint(client, unit_square_ccw)
    with pytest.raises(DBAPIError) as excinfo:
        await session.execute(
            text("UPDATE place SET external_id = 'hacked' WHERE id = :id"), {"id": geoid}
        )
        await session.flush()
    message = str(excinfo.value)
    assert "corrections mint a new geoid" in message
    assert "predecessor_id" not in message


async def test_geoid_registry_hinge_populated_on_mint(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    row = (
        (
            await session.execute(
                text(
                    "SELECT r.place_id, "
                    "       encode(r.geom_hash, 'hex') AS stored, "
                    "       encode(geoid_geom_hash_default(p.geom), 'hex') AS recomputed "
                    "FROM geoid_registry r JOIN place p ON p.id = r.place_id "
                    "WHERE r.geoid = :g"
                ),
                {"g": geoid},
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert str(row["place_id"]) == geoid
    # The relocation: the dedup geom_hash now lives on (and is enforced by)
    # geoid_registry, written by the arbiter CTE via geoid_geom_hash_default —
    # populated, 32-byte sha256, and equal to the canonical recipe over the geom.
    assert len(row["stored"]) == 64
    assert row["stored"] == row["recomputed"]


async def test_global_geom_hash_unique_lives_on_geoid_registry(session):
    # The relocated invariant: the GLOBAL geometry-dedup UNIQUE is on
    # geoid_registry (sharding-ready), not place.
    on_registry = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'uq_geoid_registry_geom_hash' "
                "AND conrelid = 'geoid_registry'::regclass"
            )
        )
    ).scalar_one()
    assert on_registry == 1
    gone_from_place = (
        await session.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_place_geom_hash'")
        )
    ).scalar_one()
    assert gone_from_place == 0


async def test_change_log_hinge_records_create(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    row = (
        await session.execute(
            text("SELECT op, payload FROM change_log WHERE geoid = :g ORDER BY seq DESC LIMIT 1"),
            {"g": geoid},
        )
    ).first()
    assert row is not None
    assert row[0] == "create"
    # The payload shape is the future federation pull-feed contract (0001 trigger);
    # pin the exact key set so a trigger change can't silently drift it.
    assert set(row[1]) == {"external_id", "originating_instance"}


async def test_duplicate_geometry_leaves_hinges_untouched(
    client, session, unit_square_ccw, unit_square_reversed
):
    await client.post("/collections/public/items", json=unit_square_ccw)
    second = await client.post("/collections/public/items", json=unit_square_reversed)
    assert second.status_code == 201  # identical geometry → the incumbent geoid, no new row

    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    registry_count = (
        await session.execute(text("SELECT count(*) FROM geoid_registry"))
    ).scalar_one()
    changelog_count = (await session.execute(text("SELECT count(*) FROM change_log"))).scalar_one()
    assert place_count == 1
    assert registry_count == 1
    assert changelog_count == 1  # the deduped second POST writes nothing

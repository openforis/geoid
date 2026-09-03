"""The dedup-recipe version stamp (migration 0003).

``dedup_recipe_stamp`` is ops bookkeeping — it records which recipe version and
which PostGIS/GEOS stack every ``geoid_registry.geom_hash`` was computed under, so an
operator can tell after an engine upgrade whether stored hashes predate the
current stack. It is deliberately NOT a data-integrity hinge: no triggers, no
ORM model, and the migration-time INSERT captures the live stack.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

# Migration 0001's inventory — the stamp table must not grow this number.
# 7 = 3 on place (after_insert, block_mutation, block_truncate) + 2×2 append-only
# guards on geoid_registry and change_log. (The BEFORE INSERT hash trigger is gone:
# the dedup hash moved to geoid_registry, written by the app's arbiter CTE.)
EXPECTED_USER_TRIGGERS = 7


async def test_initial_stamp_row_exists_with_v1(session):
    # The stamp table is append-only history: the v1 rows survive the recipe-v2
    # migration untouched.
    row = (
        await session.execute(
            text(
                "SELECT recipe_version, stamped_by, note, stamped_at "
                "FROM dedup_recipe_stamp ORDER BY id LIMIT 1"
            )
        )
    ).one()
    assert row.recipe_version == "v1"
    assert row.stamped_by == "migration:0003"
    assert "initial stamp" in row.note
    assert row.stamped_at is not None


async def test_latest_stamp_row_is_recipe_v2(session):
    # Migration 0008 (identity recipe v2) appends the current-recipe row: the
    # LATEST row answers "which recipe are the stored hashes valid under?".
    row = (
        await session.execute(
            text(
                "SELECT recipe_version, stamped_by, note "
                "FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
            )
        )
    ).one()
    assert row.recipe_version == "v2"
    assert row.stamped_by == "migration:0008"
    assert "engine-independent" in row.note


async def test_stamp_versions_match_live_stack(session):
    stamp = (
        await session.execute(
            text(
                "SELECT postgis_version, geos_version, postgis_full "
                "FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
            )
        )
    ).one()
    live = (
        await session.execute(
            text(
                "SELECT postgis_lib_version() AS lib, postgis_geos_version() AS geos, "
                "postgis_full_version() AS full"
            )
        )
    ).one()
    assert stamp.postgis_version == live.lib
    assert stamp.geos_version == live.geos
    assert stamp.postgis_full == live.full


async def test_stamp_table_has_no_triggers_and_inventory_unchanged(session):
    # D1: the stamp table is bookkeeping, not a hinge — zero triggers on it,
    # and the migration must not move the global user-trigger count.
    on_stamp = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND c.relname = 'dedup_recipe_stamp'"
            )
        )
    ).scalar_one()
    total = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE NOT t.tgisinternal AND n.nspname = 'public'"
            )
        )
    ).scalar_one()
    assert on_stamp == 0
    assert total == EXPECTED_USER_TRIGGERS

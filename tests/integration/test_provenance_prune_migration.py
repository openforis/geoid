"""Migration 0010 pin: legacy provenance rows are stripped to the 0.2 contract.

Replays the migration module's own SQL (loaded from the versions file, never
duplicated here) against a raw-inserted legacy 0.1-shaped row, then proves the
immutability trigger is back on afterwards.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0010_prune_submitted_properties.py"
)

_LEGACY_PROVENANCE = (
    '{"schema": "geoid-prov/0.1", "created_by": null, "originating_instance": "old", '
    '"client": {"name": "whisp", "version": "2.1.0"}, '
    '"submitted_properties": {"crop": "cocoa"}}'
)


def _upgrade_statements() -> list[str]:
    """The upgrade() SQL, captured by running the module with a recording op."""
    spec = importlib.util.spec_from_file_location("migration_0010", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = SimpleNamespace(execute=statements.append)
    module.upgrade()
    return statements


async def test_migration_strips_legacy_provenance_and_restores_trigger(session):
    place_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO place (id, collection_id, geom, provenance) "
            "SELECT :id, c.id, ST_GeomFromGeoJSON(:geom), CAST(:prov AS jsonb) "
            "FROM collection c WHERE c.slug = 'public'"
        ),
        {
            "id": place_id,
            "geom": '{"type":"Polygon","coordinates":[[[1,1],[2,1],[2,2],[1,2],[1,1]]]}',
            "prov": _LEGACY_PROVENANCE,
        },
    )

    for statement in _upgrade_statements():
        await session.execute(text(statement))

    stored = (
        await session.execute(text("SELECT provenance FROM place WHERE id = :id"), {"id": place_id})
    ).scalar_one()
    assert stored == {
        "schema": "geoid-prov/0.2",
        "created_by": None,
        "originating_instance": "old",
    }

    with pytest.raises(DBAPIError):
        await session.execute(
            text("UPDATE place SET external_id = 'hacked' WHERE id = :id"), {"id": place_id}
        )
        await session.flush()

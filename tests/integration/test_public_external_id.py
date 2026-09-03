"""Public default collection external_id rules (migration 0012).

Submitted external_id values are stored and echoed on reads, but in the
reserved public collection they are neither unique (duplicates mint) nor
resolvable (the external-id lookup answers an explicit 400 — never a masking
404, which this API reserves for a genuinely unknown id). Private/managed
collections keep exact uniqueness + the 409 mapping — pinned by the migrated
conflict tests in test_registry_api / test_bulk_items / test_review_fixes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _square(x: float, y: float, *, external_id: str | None = None) -> dict:
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]],
        },
        "properties": {},
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


async def test_public_duplicate_external_id_both_mint_and_store(client, session):
    a = await client.post("/collections/public/items", json=_square(0, 0, external_id="dup-pub"))
    b = await client.post("/collections/public/items", json=_square(10, 10, external_id="dup-pub"))
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text

    # The public resolver is always masked, so pin storage directly.
    for resp in (a, b):
        stored = (
            await session.execute(
                text("SELECT external_id FROM place WHERE id = :id"),
                {"id": resp.json()["geoid"]},
            )
        ).scalar_one()
        assert stored == "dup-pub"


async def test_public_external_id_lookup_answers_explicit_400(client):
    minted = await client.post(
        "/collections/public/items", json=_square(20, 20, external_id="lk-1")
    )
    assert minted.status_code == 201

    resp = await client.get("/collections/public/external/lk-1")
    assert resp.status_code == 400
    assert (
        resp.json()["message"] == "external_id lookup is not available in the public collection "
        "'public'"
    )
    # Same explicit 400 for an unknown value — the guard fires before any
    # lookup, so 404 stays reserved for genuinely unknown ids elsewhere.
    assert (await client.get("/collections/public/external/nope")).status_code == 400


async def test_external_id_unique_is_a_partial_index_excluding_public(session):
    # 0012 dropped the table constraint...
    constraint = (
        await session.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_place_collection_external_id'")
        )
    ).scalar()
    assert constraint is None

    # ...and replaced it with a UNIQUE partial index of the SAME name (the name
    # is load-bearing for the 409 classification) whose predicate excludes
    # exactly the current public collection row.
    indexdef = (
        await session.execute(
            text(
                "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_place_collection_external_id'"
            )
        )
    ).scalar_one()
    public_id = (
        await session.execute(text("SELECT id FROM collection WHERE slug = 'public'"))
    ).scalar_one()
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef
    assert str(public_id) in indexdef

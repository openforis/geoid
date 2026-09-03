"""Regression tests for the adversarial-review fixes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.integration


# --- dual-violation precedence: the geometry arbiter wins over external_id's -
# The arbiter CTE inserts the geoid_registry row first (ON CONFLICT DO NOTHING on
# the geom_hash UNIQUE); the place row — and so its external_id index — is only
# touched if the registry arbiter won. So a submission duplicating BOTH geometry
# and external_id takes the GEOMETRY path — which since the idempotent-mint change
# is a 201 carrying the incumbent geoid, NOT the external_id 409. Minted into a
# managed collection: only there can the external_id leg still conflict (the
# public one is index-excluded since 0012), so the precedence pin stays meaningful.


async def test_dual_geometry_and_external_id_duplicate_yields_incumbent_geoid(
    client, ext_collection, unit_square_ccw
):
    feature = dict(unit_square_ccw, id="dup-ext")
    base = await client.post(f"/collections/{ext_collection}/items", json=feature)
    assert base.status_code == 201

    resub = await client.post(f"/collections/{ext_collection}/items", json=feature)
    assert resub.status_code == 201
    assert resub.json() == base.json()


async def test_geometry_dup_with_another_rows_external_id_yields_incumbent_geoid(
    client, ext_collection, unit_square_ccw, other_square
):
    items = f"/collections/{ext_collection}/items"
    a = await client.post(items, json=dict(unit_square_ccw, id="ext-a"))
    b = await client.post(items, json=dict(other_square, id="ext-b"))
    assert a.status_code == 201 and b.status_code == 201

    # A's geometry + B's external_id: the geometry arbiter runs first, so the
    # response carries A's geoid and the conflicting external_id is never reached
    # (it would otherwise be a 409).
    resub = await client.post(items, json=dict(unit_square_ccw, id="ext-b"))
    assert resub.status_code == 201
    assert resub.json()["geoid"] == a.json()["geoid"]
    assert resub.json()["external_id"] == "ext-b"


# --- #4 TRUNCATE is blocked on place ----------------------------------------


async def test_place_truncate_is_blocked(client, session, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("TRUNCATE place"))
        await session.flush()


# --- #5 hinge tables are append-only ----------------------------------------


async def test_geoid_registry_delete_is_blocked(client, session, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM geoid_registry WHERE geoid = :g"), {"g": geoid})
        await session.flush()


async def test_change_log_update_is_blocked(client, session, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE change_log SET op = 'tampered'"))
        await session.flush()


def _square_at(x, y, s=0.5):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]],
        },
        "properties": {},
    }


# --- #6 GeoJSON content type -------------------------------------------------


async def test_resolver_is_geojson_media_type(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    resolver = await client.get(f"/{geoid}")
    assert resolver.headers["content-type"].startswith("application/geo+json")


# --- #7 real collection extent ----------------------------------------------


async def test_collection_extent_reflects_data(client, admin_headers):
    # The OGC describe surface is admin-gated now.
    await client.post("/collections/public/items", json=_square_at(10.0, 20.0, s=1.0))
    desc = (await client.get("/collections/public", headers=admin_headers)).json()
    assert desc["extent"]["spatial"]["bbox"][0] == [10.0, 20.0, 11.0, 21.0]


async def test_empty_collection_extent_is_world(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "emptyc"})
    desc = (await client.get("/collections/emptyc", headers=admin_headers)).json()
    assert desc["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]


# --- registry-consistency drift maps to a structured 500 (not an unhandled crash) ---


async def test_registry_consistency_drift_returns_structured_500(
    client, monkeypatch, unit_square_ccw
):
    # Force the "should never happen" drift: insert_place reports a dedup loser whose
    # incumbent collection never materialised (collection_slug=None). The service raises
    # RegistryConsistencyError and the specific-type handler returns a structured 500 —
    # cleanly, without tripping the test client's ServerErrorMiddleware re-raise.
    import uuid

    from geoid.repositories import place_repo
    from geoid.repositories.place_repo import InsertResult

    async def _drift(*args, **kwargs):
        return InsertResult(geoid=uuid.uuid4(), created=False, collection_slug=None)

    monkeypatch.setattr(place_repo, "insert_place", _drift)

    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == 500
    assert body["message"] == "internal registry inconsistency"


async def test_bulk_registry_consistency_drift_returns_structured_500(
    client, monkeypatch, unit_square_ccw
):
    # The bulk path's _mint_one carries the same drift guard: a dedup loser with no
    # incumbent collection aborts the batch with a structured 500 rather than emitting
    # a malformed reject row.
    import uuid

    from geoid.repositories import place_repo
    from geoid.repositories.place_repo import InsertResult

    async def _drift(*args, **kwargs):
        return InsertResult(geoid=uuid.uuid4(), created=False, collection_slug=None)

    monkeypatch.setattr(place_repo, "insert_place", _drift)

    fc = {"type": "FeatureCollection", "features": [unit_square_ccw]}
    resp = await client.post("/collections/public/items/bulk", json=fc)
    assert resp.status_code == 500
    assert resp.json()["message"] == "internal registry inconsistency"


# --- H4: a repeat mint returns the STORED incumbent geoid, not a re-derived one ---


async def test_repeat_mint_returns_the_stored_registry_geoid_for_legacy_rows(
    client, session, unit_square_ccw
):
    minted = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    # Simulate a legacy (pre-0004, random-UUIDv7) registry row: rewrite the stored
    # geoid, bypassing the append-only trigger (superuser test role only).
    legacy = str(uuid.uuid4())
    await session.execute(text("SET session_replication_role = replica"))
    await session.execute(
        text("UPDATE geoid_registry SET geoid = :legacy, place_id = :legacy WHERE geoid = :minted"),
        {"legacy": legacy, "minted": minted},
    )
    await session.execute(text("SET session_replication_role = origin"))
    await session.commit()

    # The repeat must report what is actually STORED (COALESCE(r.geoid, ...)),
    # never a freshly re-derived geoid that resolves nowhere.
    dup = await client.post("/collections/public/items", json=unit_square_ccw)
    assert dup.status_code == 201
    assert dup.json()["geoid"] == legacy


# --- M9: an unrecognised IntegrityError is an honest 500, not a mislabelled 409 ---


async def test_unrecognised_integrity_error_returns_500(client, monkeypatch, unit_square_ccw):
    from geoid.repositories import place_repo

    class _FakeDriverError(Exception):
        sqlstate = "23502"  # NOT NULL violation — a server bug, not a client conflict
        constraint_name = None

    async def _raise(*args, **kwargs):
        raise IntegrityError("INSERT ...", {}, _FakeDriverError("boom"))

    monkeypatch.setattr(place_repo, "insert_place", _raise)

    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 500
    assert resp.json() == {"code": 500, "message": "internal error"}


# --- H2: the DBAPIError→422 mapping is pinned to the real parse SQLSTATEs -------


async def test_st_geomfromgeojson_parse_failures_raise_a_mapped_sqlstate(session):
    # Empirical pin for GEOJSON_PARSE_SQLSTATES: malformed GeoJSON *text* reaching
    # ST_GeomFromGeoJSON must raise one of the mapped states — if a PostGIS upgrade
    # moves it, this fails and the mapping (not the 500 path) needs the update.
    from geoid.repositories._pg_errors import GEOJSON_PARSE_SQLSTATES, sqlstate_of

    with pytest.raises(DBAPIError) as exc_info:
        await session.execute(text("SELECT ST_GeomFromGeoJSON('{\"type\":')"))
    assert sqlstate_of(exc_info.value) in GEOJSON_PARSE_SQLSTATES

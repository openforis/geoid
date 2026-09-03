"""Integration tests for the write/registry surface (POST + resolvers + errors)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_post_polygon_mints_geoid_uri(client, unit_square_ccw):
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    body = resp.json()
    assert "collection" not in body  # the mint reports the geoid, not where it lives
    geoid = body["geoid"]
    assert body["uri"] == f"http://testserver/{geoid}"
    assert "item_url" not in body  # the collection-scoped item read route was removed
    # OGC API - Features Part 4, Requirement 6: a 201 carries a Location header
    # pointing at the new resource — the durable resolver, the only resolution path.
    assert resp.headers["Location"] == body["uri"]


async def test_submitted_properties_are_accepted_but_never_persisted(
    client, session, admin_headers
):
    # RFC 7946: properties (incl. the _whisp block) and unknown foreign top-level
    # members must be accepted — but geoid-prov/0.2 persists none of it.
    feature = {
        "type": "Feature",
        "id": "whisp-plot",
        "geometry": {"type": "Polygon", "coordinates": [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]]},
        "properties": {"_whisp": {"version": "2.1.0"}, "area_ha": 1.0, "crop": "cocoa"},
        "custom_member": "ignored",
    }
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]

    # The public resolver is authentication-invariant, so even a sysadmin gets
    # the same masked representation and no submitted property can leak there.
    feat = (await client.get(f"/{geoid}", headers=admin_headers)).json()
    assert set(feat["properties"]) == {"geoid", "uri"}

    # Pin STORAGE, not just presentation: the provenance jsonb is the 3-key contract.
    stored = (
        await session.execute(text("SELECT provenance FROM place WHERE id = :id"), {"id": geoid})
    ).scalar_one()
    assert stored == {
        "schema": "geoid-prov/0.2",
        "created_by": None,
        "originating_instance": "test-instance",
    }


async def test_feature_without_properties_member_mints(client):
    # RFC 7946 input leniency: an omitted properties member is treated as null, not 422.
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[3, 1], [4, 1], [4, 2], [3, 2], [3, 1]]]},
    }
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201


async def test_identical_geometry_returns_the_same_geoid(
    client, unit_square_ccw, unit_square_reversed
):
    first = await client.post("/collections/public/items", json=unit_square_ccw)
    assert first.status_code == 201
    original = first.json()

    # Same square, reversed winding + rotated ring start -> identical canonical
    # geometry -> the same geoid, answered exactly like the first mint.
    second = await client.post("/collections/public/items", json=unit_square_reversed)
    assert second.status_code == 201
    assert second.json() == original
    assert second.headers["Location"] == first.headers["Location"]


async def test_different_geometry_mints_distinct_geoid(client, unit_square_ccw, other_square):
    a = await client.post("/collections/public/items", json=unit_square_ccw)
    b = await client.post("/collections/public/items", json=other_square)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["geoid"] != b.json()["geoid"]


async def test_3d_point_rejected_422(client):
    # 2D-only is enforced at the Python validator (pre-DB), so this is stack-
    # independent — it passes on the local stack, which previously *accepted* 3D.
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0, 5]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 422


async def test_resolve_by_external_id(client, ext_collection):
    # A managed collection — the reserved public one no longer resolves by
    # external_id (tests/integration/test_public_external_id.py pins that).
    feature = {
        "type": "Feature",
        "id": "plot-xyz",
        "geometry": {"type": "Polygon", "coordinates": [[[3, 3], [4, 3], [4, 4], [3, 4], [3, 3]]]},
        "properties": {},
    }
    minted = await client.post(f"/collections/{ext_collection}/items", json=feature)
    geoid = minted.json()["geoid"]

    resp = await client.get(f"/collections/{ext_collection}/external/plot-xyz")
    assert resp.status_code == 200
    assert resp.json()["id"] == geoid


async def test_external_id_conflict_returns_409_with_constraint(
    client, ext_collection, unit_square_ccw, other_square
):
    await client.post(f"/collections/{ext_collection}/items", json={**unit_square_ccw, "id": "dup"})
    clash = await client.post(
        f"/collections/{ext_collection}/items", json={**other_square, "id": "dup"}
    )
    assert clash.status_code == 409
    assert clash.json()["constraint"] == "uq_place_collection_external_id"


async def test_invalid_self_intersecting_polygon_returns_422_with_reason(client):
    # Nonzero lattice area, so the v2 degeneracy pre-check passes it and the DB
    # ST_IsValid CHECK answers (zero-area bowties reject earlier at the schema —
    # test_identity_v2_degeneracy pins that path).
    crossed = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [0, 3], [3, 3], [0, 0]]]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=crossed)
    assert resp.status_code == 422
    assert "reason" in resp.json()


async def test_point_geometry_mints_geoid(client):
    point = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=point)
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]
    assert uuid.UUID(geoid).version == 8
    feat = (await client.get(f"/{geoid}")).json()
    assert feat["geometry"]["type"] == "Point"


async def test_multipoint_geometry_mints_geoid(client):
    mp = {
        "type": "Feature",
        "geometry": {"type": "MultiPoint", "coordinates": [[0, 0], [5, 5]]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=mp)
    assert resp.status_code == 201
    feat = (await client.get(f"/{resp.json()['geoid']}")).json()
    assert feat["geometry"]["type"] == "MultiPoint"


async def test_linestring_rejected_422(client):
    line = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=line)
    assert resp.status_code == 422


async def test_geometrycollection_rejected_422(client):
    gc = {
        "type": "Feature",
        "geometry": {
            "type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [0, 0]}],
        },
        "properties": {},
    }
    resp = await client.post("/collections/public/items", json=gc)
    assert resp.status_code == 422


async def test_unknown_collection_returns_404(client, unit_square_ccw):
    resp = await client.post("/collections/does-not-exist/items", json=unit_square_ccw)
    assert resp.status_code == 404


async def test_resolve_unknown_geoid_returns_404(client):
    resp = await client.get("/019e0000-0000-7000-8000-000000000000")
    assert resp.status_code == 404


# --- WKT string geometry input (vendor extension) ---------------------------

_WKT_SQUARE = "POLYGON((10 10,11 10,11 11,10 11,10 10))"


def _wkt_feature(wkt: str, *, external_id: str | None = None) -> dict:
    feature = {"type": "Feature", "geometry": wkt, "properties": {}}
    if external_id is not None:
        feature["id"] = external_id
    return feature


async def test_post_wkt_string_mints_geoid(client):
    resp = await client.post("/collections/public/items", json=_wkt_feature(_WKT_SQUARE))
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]
    feat = (await client.get(f"/{geoid}")).json()
    assert feat["geometry"]["type"] == "Polygon"


async def test_wkt_then_equivalent_geojson_yields_the_same_geoid(client, unit_square_ccw):
    # Parity: a WKT polygon and the equivalent GeoJSON polygon are the SAME geometry
    # -> one geoid, returned to both submissions.
    first = await client.post("/collections/public/items", json=_wkt_feature(_WKT_SQUARE))
    assert first.status_code == 201
    incumbent = first.json()["geoid"]

    second = await client.post("/collections/public/items", json=unit_square_ccw)
    assert second.status_code == 201
    assert second.json()["geoid"] == incumbent


async def test_geojson_then_equivalent_wkt_yields_the_same_geoid(client, unit_square_ccw):
    # Reverse order: GeoJSON first, then the equivalent WKT — same parity, same geoid.
    first = await client.post("/collections/public/items", json=unit_square_ccw)
    assert first.status_code == 201
    incumbent = first.json()["geoid"]

    second = await client.post("/collections/public/items", json=_wkt_feature(_WKT_SQUARE))
    assert second.status_code == 201
    assert second.json()["geoid"] == incumbent


async def test_post_invalid_wkt_returns_422(client):
    resp = await client.post("/collections/public/items", json=_wkt_feature("POLYGON((10 10,11"))
    assert resp.status_code == 422


async def test_post_ewkt_returns_422(client):
    resp = await client.post(
        "/collections/public/items", json=_wkt_feature(f"SRID=4326;{_WKT_SQUARE}")
    )
    assert resp.status_code == 422


async def test_post_wkt_point_mints_geoid(client):
    resp = await client.post("/collections/public/items", json=_wkt_feature("POINT(1 2)"))
    assert resp.status_code == 201
    feat = (await client.get(f"/{resp.json()['geoid']}")).json()
    assert feat["geometry"]["type"] == "Point"


async def test_anonymous_write_to_managed_collection_forbidden(
    client, admin_headers, unit_square_ccw
):
    await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "managed", "public_write": False},
    )
    # anonymous (no auth header) -> 403
    anon = await client.post("/collections/managed/items", json=unit_square_ccw)
    assert anon.status_code == 403
    # admin -> 201
    owned = await client.post(
        "/collections/managed/items", headers=admin_headers, json=unit_square_ccw
    )
    assert owned.status_code == 201


# --- Root-level resolver catch-all does not shadow literal routes ------------


async def test_root_resolver_does_not_shadow_literal_routes(client):
    # The durable resolver now lives at the app root (`/{geoid}`), a single-segment
    # UUID catch-all. Literal single-segment routes are registered first, so they win;
    # only genuinely-unknown single segments fall through to the resolver.
    assert (await client.get("/conformance")).status_code == 200
    # /collections is a literal route (now admin-gated) -> 401, NOT the resolver's
    # 422 — proving the literal route still wins over the `/{geoid}` catch-all.
    assert (await client.get("/collections")).status_code == 401
    # The probe and docs surfaces are literal single segments too — none may fall
    # through to the resolver (which would answer 422 for a non-UUID segment).
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/redoc")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200
    # A well-formed but unknown geoid falls through to the resolver -> 404.
    assert (await client.get("/019e0000-0000-7000-8000-000000000000")).status_code == 404
    # A non-UUID single segment can't bind the `uuid.UUID` path param -> 422.
    assert (await client.get("/not-a-uuid")).status_code == 422


# --- NUL bytes in input answer 422 (SQLSTATE 22021), never an unhandled 500 --


async def test_external_resolver_nul_path_param_returns_422(client, ext_collection):
    # The live repro: httpx passes %00 through, the decoded NUL reaches Postgres.
    # (A managed collection — on the public one the 0012 lookup guard answers
    # 400 before the NUL ever reaches a query.)
    resp = await client.get(f"/collections/{ext_collection}/external/%00")
    assert resp.status_code == 422
    assert resp.json()["message"] == "invalid characters in input (NUL)"


async def test_post_external_id_with_nul_byte_returns_422(client, unit_square_ccw):
    resp = await client.post(
        "/collections/public/items", json={**unit_square_ccw, "id": "a" + chr(0) + "b"}
    )
    assert resp.status_code == 422
    assert resp.json()["message"] == "invalid characters in input (NUL)"


async def test_bulk_nul_byte_aborts_batch_and_persists_nothing(
    client, unit_square_ccw, other_square
):
    # Batch-abort-as-422 is the accepted design: the transaction rolls back whole,
    # the client gets an honest error, and a resubmit converges via dedup.
    nul_feature = {**other_square, "id": "a" + chr(0) + "b"}
    fc = {"type": "FeatureCollection", "features": [unit_square_ccw, nul_feature]}
    resp = await client.post("/collections/public/items/bulk", json=fc)
    assert resp.status_code == 422
    assert resp.json()["message"] == "invalid characters in input (NUL)"

    # The aborted batch persisted nothing — pinned by the good feature minting a
    # geoid that no earlier row can have claimed.
    solo = await client.post("/collections/public/items", json=unit_square_ccw)
    assert solo.status_code == 201

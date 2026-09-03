"""Integration tests for the synchronous bulk mint route.

``POST /collections/{id}/items/bulk`` takes a GeoJSON FeatureCollection and mints
a geoid per feature, synchronously, with partial success: valid geometries are
inserted and bad ones are reported with the same reason a single POST returns.
The whole batch always answers 200 (the only non-200s are 413 over the cap, 404
unknown collection, 403 anonymous-into-non-writable, and a 422 envelope error).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from geoid.models import Collection, Place

pytestmark = pytest.mark.integration

_BULK = "/collections/public/items/bulk"


def _square(x: int, y: int, *, external_id: str | None = None) -> dict:
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


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


async def test_bulk_mints_distinct_resolvable_geoids(client):
    resp = await client.post(_BULK, json=_fc(_square(0, 0), _square(5, 5), _square(10, 10)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    geoids = [a["geoid"] for a in body["accepted"]]
    assert len(set(geoids)) == 3
    # Each minted geoid resolves durably and reports itself.
    for geoid in geoids:
        resolved = await client.get(f"/{geoid}")
        assert resolved.status_code == 200
        assert resolved.json()["properties"]["geoid"] == geoid


async def test_bulk_submitted_properties_are_accepted_but_never_persisted(client, session):
    # _mint_one parity with the single route: rich properties are accepted but the
    # stored provenance is exactly the geoid-prov/0.2 3-key contract.
    rich = {**_square(0, 0), "properties": {"_whisp": {"version": "2.1.0"}, "crop": "cocoa"}}
    resp = await client.post(_BULK, json=_fc(rich, _square(5, 5)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 2, "accepted": 2, "rejected": 0}

    for accepted in body["accepted"]:
        stored = (
            await session.execute(
                text("SELECT provenance FROM place WHERE id = :id"), {"id": accepted["geoid"]}
            )
        ).scalar_one()
        assert stored == {
            "schema": "geoid-prov/0.2",
            "created_by": None,
            "originating_instance": "test-instance",
        }


async def test_bulk_feature_without_properties_member_is_accepted(client):
    # The single route's RFC 7946 leniency holds per-feature: no properties member
    # is accepted, never a schema_invalid reject.
    bare = {"type": "Feature", "geometry": _square(0, 0)["geometry"]}
    resp = await client.post(_BULK, json=_fc(bare, _square(5, 5)))
    assert resp.status_code == 200
    assert resp.json()["summary"] == {"received": 2, "accepted": 2, "rejected": 0}


async def test_bulk_geoid_matches_single_route_hash(client, unit_square_ccw):
    # Hash parity: the same geometry minted via the single route comes back from the
    # bulk route carrying that same geoid — proving both paths compute the identical
    # geom_hash.
    single = await client.post("/collections/public/items", json=unit_square_ccw)
    assert single.status_code == 201
    incumbent = single.json()["geoid"]

    resp = await client.post(_BULK, json=_fc(unit_square_ccw))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 1, "accepted": 1, "rejected": 0}
    assert body["accepted"][0]["geoid"] == incumbent


async def test_bulk_in_batch_geometry_twins_collapse_onto_one_geoid(
    client, unit_square_ccw, unit_square_reversed, other_square
):
    # Two encodings of the same canonical geometry in one batch: the first mints,
    # the second collapses onto it (the arbiter sees the in-batch row) and is
    # ACCEPTED with that geoid, the third (distinct) mints its own. No SAVEPOINT
    # poisoning — the batch survives, and `accepted` carries a repeated geoid.
    resp = await client.post(_BULK, json=_fc(unit_square_ccw, unit_square_reversed, other_square))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    by_index = {a["index"]: a["geoid"] for a in body["accepted"]}
    assert by_index[0] == by_index[1]
    assert by_index[2] != by_index[0]


async def test_bulk_resubmitting_a_whole_batch_accepts_everything_again(
    client, unit_square_ccw, other_square
):
    first = await client.post(_BULK, json=_fc(unit_square_ccw, other_square))
    assert first.json()["summary"] == {"received": 2, "accepted": 2, "rejected": 0}
    minted = {a["index"]: a["geoid"] for a in first.json()["accepted"]}

    again = await client.post(_BULK, json=_fc(unit_square_ccw, other_square))
    body = again.json()
    assert body["summary"] == {"received": 2, "accepted": 2, "rejected": 0}
    assert {a["index"]: a["geoid"] for a in body["accepted"]} == minted


async def test_bulk_external_id_conflict_in_batch(client, ext_collection):
    # external_id uniqueness applies only outside the reserved public
    # collection (0012) — these conflict tests mint into a managed one.
    resp = await client.post(
        f"/collections/{ext_collection}/items/bulk",
        json=_fc(_square(0, 0, external_id="dup"), _square(10, 10, external_id="dup")),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 2, "accepted": 1, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "external_id_conflict"
    assert rejected["external_id"] == "dup"


async def test_bulk_external_id_conflict_against_existing(client, ext_collection):
    first = await client.post(
        f"/collections/{ext_collection}/items", json=_square(0, 0, external_id="ext1")
    )
    assert first.status_code == 201
    # Different geometry, same external_id already taken in the collection -> reject.
    resp = await client.post(
        f"/collections/{ext_collection}/items/bulk", json=_fc(_square(10, 10, external_id="ext1"))
    )
    body = resp.json()
    assert body["summary"] == {"received": 1, "accepted": 0, "rejected": 1}
    assert body["rejected"][0]["reason"] == "external_id_conflict"


async def test_bulk_invalid_geometry_rejected_batch_continues(client):
    # A self-intersecting polygon with NONZERO lattice area passes the PlaceCreate
    # schema (valid RFC 7946 structure; not lattice-degenerate, so the v2 pre-check
    # lets it through) but fails the ST_IsValid DB CHECK -> invalid_geometry, and
    # the surrounding valid features still mint. (A zero-area bowtie now rejects
    # earlier at the schema as identity-degenerate — test_identity_v2_degeneracy.)
    crossed = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [0, 3], [3, 3], [0, 0]]]},
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(10, 10), crossed, _square(20, 20)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "invalid_geometry"
    assert rejected["detail"]  # carries ST_IsValidReason


async def test_bulk_schema_invalid_rejected_batch_continues(client):
    # A LineString fails the supported-geometry PlaceCreate schema -> one
    # schema_invalid row, never a 422 that sinks the whole request.
    line = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(10, 10), line, _square(20, 20)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "schema_invalid"


async def test_bulk_mints_mixed_point_and_polygon(client):
    # Point, MultiPoint, and Polygon all mint in one batch (all supported types).
    point = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [40, 40]},
        "properties": {},
    }
    multipoint = {
        "type": "Feature",
        "geometry": {"type": "MultiPoint", "coordinates": [[41, 41], [42, 42]]},
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(point, multipoint, _square(50, 50)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    types = set()
    for accepted in body["accepted"]:
        feat = (await client.get(f"/{accepted['geoid']}")).json()
        types.add(feat["geometry"]["type"])
    assert types == {"Point", "MultiPoint", "Polygon"}


async def test_bulk_accepts_wkt_string_geometry(client):
    # A WKT-string geometry is decoded by PlaceCreate, so it mints alongside GeoJSON
    # in the same batch (vendor extension; identical canonicalisation).
    wkt_feature = {
        "type": "Feature",
        "geometry": "POLYGON ((50 50, 51 50, 51 51, 50 51, 50 50))",
        "properties": {},
    }
    resp = await client.post(_BULK, json=_fc(_square(0, 0), wkt_feature))
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 2, "accepted": 2, "rejected": 0}
    wkt_geoid = next(a["geoid"] for a in body["accepted"] if a["index"] == 1)
    assert (await client.get(f"/{wkt_geoid}")).status_code == 200


async def test_bulk_external_id_conflict_mid_batch_recovers(client, ext_collection):
    # [ext-X, ext-X dup, fresh]: the middle abort rolls back only its SAVEPOINT, so
    # the batch RECOVERS and the LATER feature still mints — pins abort-then-recover
    # ordering (the other external_id tests put the failing feature last).
    resp = await client.post(
        f"/collections/{ext_collection}/items/bulk",
        json=_fc(
            _square(0, 0, external_id="ext-X"),
            _square(10, 10, external_id="ext-X"),
            _square(20, 20, external_id="fresh"),
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"received": 3, "accepted": 2, "rejected": 1}
    rejected = body["rejected"][0]
    assert rejected["index"] == 1
    assert rejected["reason"] == "external_id_conflict"
    assert {a["index"] for a in body["accepted"]} == {0, 2}


@pytest.fixture
async def capped_client(db_clean):
    """A client whose app sees a tiny bulk cap (3), for exact-boundary tests."""
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, bulk_max_features=3)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def test_bulk_exactly_at_cap_succeeds(capped_client):
    # The cap is inclusive-at, exclusive-over: exactly cap features mint fine.
    at_cap = await capped_client.post(
        _BULK, json=_fc(_square(0, 0), _square(5, 5), _square(10, 10))
    )
    assert at_cap.status_code == 200
    assert at_cap.json()["summary"]["accepted"] == 3

    over = await capped_client.post(_BULK, json=_fc(*[_square(i * 2, 20) for i in range(4)]))
    assert over.status_code == 413
    assert over.json()["limit"] == 3


async def test_bulk_over_limit_returns_413(client):
    # A write bound MUST error, never truncate. The cap is checked before any insert.
    resp = await client.post(_BULK, json=_fc(*[_square(0, 0) for _ in range(1001)]))
    assert resp.status_code == 413
    body = resp.json()
    assert body["count"] == 1001
    assert body["limit"] == 1000


async def test_bulk_empty_feature_collection_is_422(client):
    resp = await client.post(_BULK, json={"type": "FeatureCollection", "features": []})
    assert resp.status_code == 422


async def test_bulk_unknown_collection_404(client):
    resp = await client.post("/collections/ghost/items/bulk", json=_fc(_square(0, 0)))
    assert resp.status_code == 404


async def test_bulk_anonymous_into_non_writable_collection_403(client, admin_headers, session):
    await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "locked", "public_write": False},
    )
    # Anonymous (no token) into a non-public_write collection -> fail-fast 403.
    resp = await client.post(
        "/collections/locked/items/bulk", json=_fc(_square(0, 0), _square(5, 5))
    )
    assert resp.status_code == 403
    assert resp.json()["collection"] == "locked"
    # Fail-fast: the 403 happens before any insert, so nothing was written. (The
    # item-ids listing is gone, so confirm via a direct ORM row count instead.)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Place)
            .join(Collection, Place.collection_id == Collection.id)
            .where(Collection.slug == "locked")
        )
    ).scalar_one()
    assert count == 0

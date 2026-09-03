"""Integration tests for the management slice."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_manage_requires_auth(client):
    assert (await client.get("/manage/collections")).status_code == 401


async def test_manage_rejects_wrong_token(client):
    resp = await client.get("/manage/collections", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_create_collection(client, admin_headers):
    coll = await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "eudr", "title": "EUDR plots", "public_write": False},
    )
    assert coll.status_code == 201
    body = coll.json()
    assert body["id"] == "eudr"
    assert body["public_write"] is False
    # The catalog tier is hidden: no internal UUID, no catalog_id in the response.
    assert "catalog_id" not in body
    assert set(body) == {"id", "title", "public_write", "public_read", "metadata"}


async def test_create_collection_id_round_trips_in_url(client, admin_headers):
    # The 404 trap is gone: the response `id` is exactly the URL segment. The OGC
    # describe surface is admin-gated, so the round-trip GET carries the admin header.
    coll = await client.post(
        "/manage/collections", headers=admin_headers, json={"id": "land-parcels"}
    )
    collection_id = coll.json()["id"]
    resp = await client.get(f"/collections/{collection_id}", headers=admin_headers)
    assert resp.status_code == 200


async def test_duplicate_collection_id_returns_409(client, admin_headers):
    first = await client.post("/manage/collections", headers=admin_headers, json={"id": "dupe"})
    assert first.status_code == 201
    second = await client.post("/manage/collections", headers=admin_headers, json={"id": "dupe"})
    assert second.status_code == 409
    body = second.json()
    assert body["message"] == "collection id already exists"
    assert body["constraint"] == "uq_collection_catalog_slug"


async def test_catalog_routes_are_gone(client, admin_headers):
    # Catalog is bootstrap-created and internal; there is no /manage/catalogs surface.
    assert (await client.get("/manage/catalogs", headers=admin_headers)).status_code == 404
    resp = await client.post("/manage/catalogs", headers=admin_headers, json={"id": "x"})
    assert resp.status_code == 404


async def test_list_collections_slice(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "a"})
    await client.post("/manage/collections", headers=admin_headers, json={"id": "b"})

    body = (await client.get("/manage/collections", headers=admin_headers)).json()
    # The bootstrap `public` collection is always present alongside the two created here.
    assert {c["id"] for c in body} == {"public", "a", "b"}


async def test_collection_create_rejects_any_dedup_grid(client, admin_headers):
    # Geometry dedup is global: there is no per-collection grid, so the key is
    # rejected outright (422) rather than silently ignored — silently dropping
    # it would let an admin believe an override took.
    for bad in ("10m", None, True, [0.0001], 0, -1e-7, 1e-7, 1e-6):
        resp = await client.post(
            "/manage/collections",
            headers=admin_headers,
            json={"id": "v", "metadata": {"dedup_grid": bad}},
        )
        assert resp.status_code == 422, f"dedup_grid={bad!r} was accepted"


async def test_create_collection_via_service(session):
    # 2.1: the route now delegates to listing_service.create_collection. Exercise the
    # service directly so the service-layer write path is pinned (not just the route),
    # including the default-catalog resolution and the persisted, listable result.
    from geoid.schemas.collection import CollectionCreate, CollectionOut
    from geoid.services import listing_service

    out = await listing_service.create_collection(
        session, CollectionCreate(id="svc-made", title="Service-made")
    )
    assert isinstance(out, CollectionOut)
    assert out.id == "svc-made"
    assert out.title == "Service-made"

    listed = await listing_service.list_collections(session)
    assert "svc-made" in {c.id for c in listed}

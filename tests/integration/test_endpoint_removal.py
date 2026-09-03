"""Regression matrix for the item-listing removal + collections admin-gating.

No endpoint may enumerate / filter / search a collection's items. The OGC
collections read surface is gated behind the admin key (matching
``GET /manage/collections``); landing + ``/conformance`` stay public.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# --- collections read surface is admin-gated --------------------------------


async def test_collections_list_requires_admin(client, admin_headers):
    assert (await client.get("/collections")).status_code == 401
    ok = await client.get("/collections", headers=admin_headers)
    assert ok.status_code == 200
    body = ok.json()
    assert "collections" in body
    assert "public" in {c["id"] for c in body["collections"]}


async def test_collection_describe_requires_admin(client, admin_headers):
    assert (await client.get("/collections/public")).status_code == 401
    ok = await client.get("/collections/public", headers=admin_headers)
    assert ok.status_code == 200
    assert ok.json()["id"] == "public"


# --- the item read surface is gone (404) ------------------------------------


async def test_items_listing_is_removed(client):
    # POST /collections/{id}/items (mint) still owns this path, so a GET listing is
    # 405 Method Not Allowed — there is no read method on the items collection.
    assert (await client.get("/collections/public/items")).status_code == 405


async def test_items_filtering_is_removed(client):
    assert (await client.get("/collections/public/items?filter=external_id='x'")).status_code == 405


async def test_single_item_lookup_is_removed(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    assert (await client.get(f"/collections/public/items/{geoid}")).status_code == 404


async def test_queryables_is_removed(client):
    # Both the canonical route and its former /items/queryables alias are gone.
    assert (await client.get("/collections/public/queryables")).status_code == 404
    assert (await client.get("/collections/public/items/queryables")).status_code == 404


async def test_item_ids_enumeration_is_removed(client, admin_headers):
    # Even admin-only item enumeration is gone.
    resp = await client.get("/manage/collections/public/item-ids", headers=admin_headers)
    assert resp.status_code == 404


# --- the OpenAPI definition and conformance reflect the removal -------------


async def test_openapi_has_no_item_read_routes(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    # POST /collections/{id}/items (write) survives — assert the GET method is gone.
    assert "get" not in paths.get("/collections/{collection_id}/items", {})
    # The single-feature GET path was GET-only, so the whole path is gone.
    assert "/collections/{collection_id}/items/{geoid}" not in paths


async def test_conformance_drops_filter_and_cql2_classes(client):
    classes = (await client.get("/conformance")).json()["conformsTo"]
    assert any(c.endswith("ogcapi-features-1/1.0/conf/core") for c in classes)
    joined = " ".join(classes).lower()
    assert "cql2" not in joined
    assert "filter" not in joined
    assert "queryables" not in joined


# --- the surviving public surface still works -------------------------------


async def test_landing_is_public(client):
    body = (await client.get("/")).json()
    assert {"self", "conformance", "data"} <= {link["rel"] for link in body["links"]}

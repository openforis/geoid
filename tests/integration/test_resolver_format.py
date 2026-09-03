"""Format negotiation on the durable resolver ``GET /{geoid}``.

Ported from the deleted item-read tests: the items endpoint is gone, so the only
single-feature WKT/GeoJSON negotiation path left is the root resolver. These pin
``output_format`` + ``feature_response`` (the shared single-feature render path).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def _mint(client, feature) -> str:
    return (await client.post("/collections/public/items", json=feature)).json()["geoid"]


def _feature(coords, geom_type: str) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": {},
    }


@pytest.mark.parametrize(
    ("coords", "geom_type", "wkt_prefix"),
    [
        ([0, 0], "Point", "POINT"),
        ([[0, 0], [5, 5]], "MultiPoint", "MULTIPOINT"),
        ([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], "Polygon", "POLYGON"),
    ],
)
async def test_resolver_round_trips_supported_types(client, coords, geom_type, wkt_prefix):
    # Every supported geometry type resolves in both encodings: GeoJSON type echoed,
    # WKT body carries the matching prefix.
    geoid = await _mint(client, _feature(coords, geom_type))
    geojson = await client.get(f"/{geoid}")
    assert geojson.status_code == 200
    assert geojson.json()["geometry"]["type"] == geom_type
    wkt = await client.get(f"/{geoid}?f=wkt")
    assert wkt.status_code == 200
    assert wkt.text.startswith(wkt_prefix)


async def test_resolver_default_is_geojson_with_wkt_alternate(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["geometry"]["type"] == "Polygon"
    # self is the resolver; the GeoJSON feature advertises its WKT alternate.
    self_link = next(link for link in body["links"] if link["rel"] == "self")
    assert self_link["href"].endswith(f"/{geoid}")
    alts = [link for link in body["links"] if link["rel"] == "alternate"]
    assert any(link.get("type") == "text/plain" and "f=wkt" in link["href"] for link in alts)


async def test_resolver_as_wkt_via_f_param(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}?f=wkt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("POLYGON")
    # Reciprocal alternate: the bare-WKT body points back at its GeoJSON form.
    link = resp.headers["link"]
    assert 'rel="alternate"' in link
    assert "application/geo+json" in link


async def test_resolver_as_wkt_via_format_alias(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}?format=wkt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("POLYGON")


async def test_resolver_as_wkt_via_accept_header(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}", headers={"Accept": "text/plain"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("POLYGON")


async def test_format_param_wins_over_f(client, unit_square_ccw):
    # When both are given, `format` takes precedence -> GeoJSON.
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}?format=geojson&f=wkt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")


async def test_f_param_overrides_accept_header(client, unit_square_ccw):
    # ?f=geojson must win over Accept: text/plain -> GeoJSON, not WKT.
    geoid = await _mint(client, unit_square_ccw)
    resp = await client.get(f"/{geoid}?f=geojson", headers={"Accept": "text/plain"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    assert resp.json()["geometry"]["type"] == "Polygon"


async def test_unknown_f_param_returns_400(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    assert (await client.get(f"/{geoid}?f=xml")).status_code == 400


async def test_unknown_format_param_returns_400(client, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    assert (await client.get(f"/{geoid}?format=xml")).status_code == 400


async def test_external_id_resolver_negotiates_wkt(client, ext_collection, unit_square_ccw):
    # The (external_id, collection) resolver shares the same output_format
    # dependency as the root resolver — pin that WKT negotiation works there too.
    # (A managed collection: the public one no longer resolves by external_id.)
    feature = dict(unit_square_ccw, id="ext-wkt")
    minted = await client.post(f"/collections/{ext_collection}/items", json=feature)
    assert minted.status_code == 201

    resp = await client.get(f"/collections/{ext_collection}/external/ext-wkt?format=wkt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("POLYGON")
    # And the default stays GeoJSON.
    default = await client.get(f"/collections/{ext_collection}/external/ext-wkt")
    assert default.headers["content-type"].startswith("application/geo+json")


async def test_format_advertised_in_openapi_not_f(client):
    # The resolver advertises the friendly `format` param; the OGC `f` alias is
    # accepted but hidden from the schema (ported from the deleted item-read tests).
    params = (await client.get("/openapi.json")).json()["paths"]["/{geoid}"]["get"]["parameters"]
    names = {p["name"] for p in params}
    assert "format" in names
    assert "f" not in names

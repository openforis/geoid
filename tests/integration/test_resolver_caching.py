"""Integration tests for the flag-gated resolver HTTP-caching trial.

``GEOID_RESOLVER_CACHE_MAX_AGE`` > 0 (the ``cache_client`` fixture pins 3600)
adds a strong per-representation ETag ``"{geoid}:{format}:{salt}"`` +
Cache-Control + Vary to anonymous 200s. The public ``/{geoid}`` resolver ignores
authentication, so every caller gets the same publicly cacheable representation;
the hidden external-id resolver remains caller-aware and gives authenticated
responses ``private, no-store``. Matching public validators answer a header-identical
empty 304. The default 0 keeps cache headers disabled.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.integration

CACHE_HEADERS = ("ETag", "Cache-Control", "Vary")
EXTERNAL_ID = "cache-probe"


def _square() -> dict:
    return {
        "type": "Feature",
        "id": EXTERNAL_ID,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
        },
        "properties": {},
    }


@pytest.fixture
def mint_probe(ext_collection):
    """Mint one place; return (geoid, geoid_resolver_path, external_resolver_path).

    Mints into a managed collection: the external-id resolver no longer answers
    in the reserved public one (0012), and the cache-header behavior under test
    is collection-agnostic.
    """

    async def _probe(client) -> tuple[str, str, str]:
        resp = await client.post(f"/collections/{ext_collection}/items", json=_square())
        assert resp.status_code == 201
        geoid = resp.json()["geoid"]
        return geoid, f"/{geoid}", f"/collections/{ext_collection}/external/{EXTERNAL_ID}"

    return _probe


# --- Flag off (default 0): byte-identical behavior --------------------------


async def test_flag_off_resolvers_carry_no_cache_headers(client, mint_probe):
    _, geoid_path, external_path = await mint_probe(client)
    for path in (geoid_path, external_path):
        resp = await client.get(path)
        assert resp.status_code == 200
        for header in CACHE_HEADERS:
            assert header not in resp.headers


async def test_flag_off_ignores_if_none_match(client, mint_probe):
    _, geoid_path, _ = await mint_probe(client)
    resp = await client.get(geoid_path, headers={"If-None-Match": "*"})
    assert resp.status_code == 200


async def test_flag_off_openapi_has_no_if_none_match_parameter(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "if-none-match" not in json.dumps(resp.json()).lower()


async def test_openapi_is_flag_invariant(oidc_client, cache_client):
    # The two fixtures differ ONLY in resolver_cache_max_age (0 vs 3600):
    # the flag must never leak into the schema.
    off = (await oidc_client.get("/openapi.json")).json()
    on = (await cache_client.get("/openapi.json")).json()
    assert off == on


# --- Anonymous 200s: ETag + Cache-Control + Vary -----------------------------


async def test_anon_geojson_200_carries_cache_headers_same_etag_on_both_routes(
    cache_client, mint_probe
):
    geoid, geoid_path, external_path = await mint_probe(cache_client)
    etags = []
    for path in (geoid_path, external_path):
        resp = await cache_client.get(path)
        assert resp.status_code == 200
        assert resp.headers["ETag"].startswith(f'"{geoid}:geojson:')
        assert resp.headers["ETag"].endswith('"')
        assert resp.headers["Cache-Control"] == "public, max-age=3600"
        expected_vary = "Accept" if path == geoid_path else "Accept, Authorization"
        assert resp.headers["Vary"] == expected_vary
        etags.append(resp.headers["ETag"])
    assert etags[0] == etags[1]  # one place, one GeoJSON ETag — on either route


async def test_wkt_etag_is_per_representation(cache_client, mint_probe):
    geoid, geoid_path, _ = await mint_probe(cache_client)
    geojson_etag = (await cache_client.get(geoid_path)).headers["ETag"]

    via_query = await cache_client.get(geoid_path, params={"f": "wkt"})
    via_accept = await cache_client.get(geoid_path, headers={"Accept": "text/plain"})
    for resp in (via_query, via_accept):
        assert resp.status_code == 200
        assert resp.headers["ETag"].startswith(f'"{geoid}:wkt:')
        assert resp.headers["ETag"] != geojson_etag
        assert resp.headers["Cache-Control"] == "public, max-age=3600"
        assert resp.headers["Vary"] == "Accept"
        assert "Link" in resp.headers  # the GeoJSON alternate survives the merge
    assert via_query.headers["ETag"] == via_accept.headers["ETag"]


# --- If-None-Match revalidation ----------------------------------------------


@pytest.mark.parametrize("params", [{}, {"f": "wkt"}], ids=["geojson", "wkt"])
@pytest.mark.parametrize("route", ["geoid", "external"])
async def test_if_none_match_hit_returns_header_identical_empty_304(
    cache_client, mint_probe, route, params
):
    _, geoid_path, external_path = await mint_probe(cache_client)
    path = geoid_path if route == "geoid" else external_path
    ok = await cache_client.get(path, params=params)
    assert ok.status_code == 200

    revalidated = await cache_client.get(
        path, params=params, headers={"If-None-Match": ok.headers["ETag"]}
    )
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    for header in CACHE_HEADERS:
        assert revalidated.headers[header] == ok.headers[header]


@pytest.mark.parametrize(
    "template",
    ['"unrelated", {etag}', "W/{etag}", 'W/"unrelated", {etag}', "*"],
    ids=["comma-list", "weak-prefix", "weak-list", "star"],
)
async def test_if_none_match_tolerates_rfc9110_forms(cache_client, mint_probe, template):
    _, geoid_path, _ = await mint_probe(cache_client)
    etag = (await cache_client.get(geoid_path)).headers["ETag"]
    header = template.format(etag=etag)
    resp = await cache_client.get(geoid_path, headers={"If-None-Match": header})
    assert resp.status_code == 304


def test_etag_salt_rotates_with_version_and_base_url():
    from geoid.api.responses import _representation_salt

    assert _representation_salt("https://a.example") != _representation_salt("https://b.example")
    assert _representation_salt("https://a.example") == _representation_salt("https://a.example")


async def test_non_matching_if_none_match_answers_200(cache_client, mint_probe):
    _, geoid_path, _ = await mint_probe(cache_client)
    resp = await cache_client.get(geoid_path, headers={"If-None-Match": '"stale-etag"'})
    assert resp.status_code == 200
    assert "ETag" in resp.headers


# --- Authentication is inert publicly; hidden resolver stays caller-aware ----


@pytest.mark.parametrize("params", [{}, {"f": "wkt"}], ids=["geojson", "wkt"])
async def test_authed_public_response_matches_anon_while_hidden_resolver_is_private(
    cache_client, mint_probe, make_token, bearer, params
):
    _, geoid_path, external_path = await mint_probe(cache_client)
    headers = bearer(make_token(sub="kc-reader"))
    anonymous = await cache_client.get(geoid_path, params=params)
    authenticated = await cache_client.get(geoid_path, params=params, headers=headers)
    assert authenticated.status_code == anonymous.status_code == 200
    assert authenticated.content == anonymous.content
    for header in CACHE_HEADERS:
        assert authenticated.headers[header] == anonymous.headers[header]

    hidden = await cache_client.get(external_path, params=params, headers=headers)
    assert hidden.status_code == 200
    assert hidden.headers["Cache-Control"] == "private, no-store"
    assert "ETag" not in hidden.headers
    assert "Vary" not in hidden.headers


async def test_public_resolver_masks_creator_and_is_publicly_cacheable(
    cache_client, make_token, bearer
):
    headers = bearer(make_token(sub="kc-creator"))
    minted = await cache_client.post("/collections/public/items", json=_square(), headers=headers)
    assert minted.status_code == 201

    response = await cache_client.get(f"/{minted.json()['geoid']}", headers=headers)
    assert response.status_code == 200
    assert set(response.json()["properties"]) == {"geoid", "uri"}
    assert response.headers["Cache-Control"] == "public, max-age=3600"
    assert "ETag" in response.headers
    assert response.headers["Vary"] == "Accept"


async def test_authed_replay_of_public_etag_returns_304(
    cache_client, mint_probe, make_token, bearer
):
    _, geoid_path, _ = await mint_probe(cache_client)
    anon_etag = (await cache_client.get(geoid_path)).headers["ETag"]

    resp = await cache_client.get(
        geoid_path,
        headers={**bearer(make_token(sub="kc-reader")), "If-None-Match": anon_etag},
    )
    assert resp.status_code == 304
    assert resp.headers["Cache-Control"] == "public, max-age=3600"


# --- Error paths stay untouched ----------------------------------------------


async def test_404_paths_carry_no_cache_headers(cache_client, ext_collection):
    for path in (f"/{uuid.uuid4()}", f"/collections/{ext_collection}/external/no-such-id"):
        resp = await cache_client.get(path)
        assert resp.status_code == 404
        for header in CACHE_HEADERS:
            assert header not in resp.headers
    # The public collection's disabled lookup (400) is equally uncacheable.
    guard = await cache_client.get("/collections/public/external/no-such-id")
    assert guard.status_code == 400
    for header in CACHE_HEADERS:
        assert header not in guard.headers


async def test_400_unknown_format_carries_no_cache_headers(cache_client, mint_probe):
    _, geoid_path, _ = await mint_probe(cache_client)
    resp = await cache_client.get(geoid_path, params={"format": "bogus"})
    assert resp.status_code == 400
    for header in CACHE_HEADERS:
        assert header not in resp.headers


async def test_422_non_uuid_segment_carries_no_cache_headers(cache_client):
    resp = await cache_client.get("/not-a-uuid")
    assert resp.status_code == 422
    for header in CACHE_HEADERS:
        assert header not in resp.headers

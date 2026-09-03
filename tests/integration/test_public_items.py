"""The three public operations, end to end — the client-facing contract.

``POST /items``, ``POST /items/bulk`` and ``GET /{geoid}`` are the only operations
in the published schema (client meeting 2026-07-25). They ignore authentication:
valid, invalid, expired, and absent credentials have one wire contract, and public
mints record anonymous provenance. Each write operation is ONE handler served at
two paths — the public one (hard-wired to the reserved public collection) and the
collection-scoped one, which stays live but hidden. The executable rules this file
pins also include idempotence and success/failure parity between those two paths.
"""

from __future__ import annotations

import contextlib

import pytest

pytestmark = pytest.mark.integration

_ALT_PUBLIC = "alt-public"


@contextlib.asynccontextmanager
async def _client_for_settings(**overrides):
    """A client whose settings differ from the defaults (e.g. a renamed public collection)."""
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **overrides)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def _square(x: float, y: float, *, external_id: str | None = None) -> dict:
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]],
        },
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


async def test_post_items_mints_and_repeats_identically(client):
    feature = _square(11, 11)

    first = await client.post("/items", json=feature)
    assert first.status_code == 201
    body = first.json()
    assert set(body) == {"geoid", "uri", "external_id"}
    assert body["uri"] == f"http://testserver/{body['geoid']}"
    assert first.headers["Location"] == body["uri"]

    second = await client.post("/items", json=feature)
    assert second.status_code == 201
    assert second.json() == body
    assert second.headers["Location"] == body["uri"]
    # No duplicate signal of any kind: no conflict/constraint/duplicate field.
    assert not {"constraint", "collection", "duplicate", "deduplicated"} & set(second.json())


async def test_post_items_lands_in_the_public_collection(client, admin_headers):
    # The alias is hard-wired to settings.public_collection — pinned via the
    # sysadmin inventory rather than the response, which names no collection.
    minted = (await client.post("/items", json=_square(12, 12))).json()
    listed = (await client.get("/manage/collections/public/items", headers=admin_headers)).json()
    assert minted["geoid"] in {row["geoid"] for row in listed}


async def test_post_items_resolves_as_geojson_and_wkt(client):
    geoid = (await client.post("/items", json=_square(13, 13))).json()["geoid"]

    geojson = await client.get(f"/{geoid}")
    assert geojson.status_code == 200
    assert geojson.json()["geometry"]["type"] == "Polygon"

    wkt = await client.get(f"/{geoid}", params={"format": "wkt"})
    assert wkt.status_code == 200
    assert wkt.text.startswith("POLYGON")


async def test_post_items_bulk_accepts_in_batch_twins_and_resubmissions(client):
    twin = _square(14, 14)
    fc = {"type": "FeatureCollection", "features": [twin, twin, _square(16, 16)]}

    first = await client.post("/items/bulk", json=fc)
    assert first.status_code == 200
    body = first.json()
    assert body["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    by_index = {row["index"]: row["geoid"] for row in body["accepted"]}
    assert by_index[0] == by_index[1] != by_index[2]

    again = await client.post("/items/bulk", json=fc)
    assert again.json()["summary"] == {"received": 3, "accepted": 3, "rejected": 0}
    assert {r["index"]: r["geoid"] for r in again.json()["accepted"]} == by_index


async def test_public_aliases_agree_with_the_collection_scoped_routes(client):
    # One handler, two paths: the two URLs must return the same wire response.
    feature = _square(17, 17)
    alias = await client.post("/items", json=feature)
    scoped = await client.post("/collections/public/items", json=feature)
    assert alias.status_code == scoped.status_code == 201
    assert alias.content == scoped.content
    assert alias.headers["content-type"] == scoped.headers["content-type"]
    # Location is the single-item 201 contract; the bulk report carries none.
    assert alias.headers["Location"] == scoped.headers["Location"]


async def test_public_bulk_alias_agrees_with_the_collection_scoped_route(client):
    feature_collection = {"type": "FeatureCollection", "features": [_square(22, 22)]}

    alias = await client.post("/items/bulk", json=feature_collection)
    scoped = await client.post("/collections/public/items/bulk", json=feature_collection)

    assert alias.status_code == scoped.status_code == 200
    assert alias.content == scoped.content
    assert alias.headers["content-type"] == scoped.headers["content-type"]
    assert alias.json()["summary"] == {"received": 1, "accepted": 1, "rejected": 0}


async def test_the_aliases_follow_the_configured_public_collection(client, admin_headers):
    # Both aliases read settings.public_collection, so a deployment that renames it
    # via GEOID_PUBLIC_COLLECTION is followed — a hardcoded "public" would pass every
    # other test in this file.
    created = await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": _ALT_PUBLIC, "public_write": True},
    )
    assert created.status_code == 201, created.text

    async with _client_for_settings(public_collection=_ALT_PUBLIC) as alt_client:
        single = await alt_client.post("/items", json=_square(23, 23))
        bulk = await alt_client.post(
            "/items/bulk", json={"type": "FeatureCollection", "features": [_square(24, 24)]}
        )
    assert single.status_code == 201, single.text
    assert bulk.status_code == 200, bulk.text

    listed = await client.get(f"/manage/collections/{_ALT_PUBLIC}/items", headers=admin_headers)
    landed = {row["geoid"] for row in listed.json()}
    assert single.json()["geoid"] in landed
    assert bulk.json()["accepted"][0]["geoid"] in landed


async def test_the_public_path_cannot_be_aimed_at_another_collection(client, admin_headers):
    # The scoped collection_id is read from the PATH only. A query parameter of the
    # same name is not a parameter of these operations and must be inert.
    created = await client.post(
        "/manage/collections", headers=admin_headers, json={"id": "elsewhere", "public_write": True}
    )
    assert created.status_code == 201, created.text

    minted = await client.post("/items?collection_id=elsewhere", json=_square(25, 25))
    assert minted.status_code == 201

    listed = await client.get("/manage/collections/elsewhere/items", headers=admin_headers)
    assert listed.json() == []


@pytest.mark.parametrize("suffix", ["", "/bulk"])
async def test_the_two_paths_reject_a_malformed_body_identically(client, suffix):
    # Equality has to hold on the failure paths too — that is where a second
    # decorator's validation contract would diverge first.
    body = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": "nonsense"}}

    alias = await client.post(f"/items{suffix}", json=body)
    scoped = await client.post(f"/collections/public/items{suffix}", json=body)

    assert alias.status_code == scoped.status_code == 422
    assert alias.json() == scoped.json()
    assert alias.headers["content-type"] == scoped.headers["content-type"]


async def test_the_two_paths_enforce_the_bulk_limit_identically(client):
    features = [_square(26, 26), _square(27, 27)]
    fc = {"type": "FeatureCollection", "features": features}

    async with _client_for_settings(bulk_max_features=1) as capped:
        alias = await capped.post("/items/bulk", json=fc)
        scoped = await capped.post("/collections/public/items/bulk", json=fc)

    assert alias.status_code == scoped.status_code == 413
    assert alias.json() == scoped.json()


async def test_public_external_id_is_echoed_but_never_unique(client):
    a = await client.post("/items", json=_square(18, 18, external_id="plot-1"))
    b = await client.post("/items", json=_square(19, 19, external_id="plot-1"))
    assert a.status_code == b.status_code == 201
    assert a.json()["external_id"] == b.json()["external_id"] == "plot-1"
    assert a.json()["geoid"] != b.json()["geoid"]


async def test_anonymous_writes_need_no_credential(client):
    assert (await client.post("/items", json=_square(20, 20))).status_code == 201


async def test_the_three_public_operations_ignore_all_credentials(oidc_client, make_token, bearer):
    valid = bearer(make_token(sub="kc-public"))
    expired = bearer(make_token(sub="kc-expired", exp_delta=-3600))
    malformed = bearer("not.a.jwt")
    credential_variants = (None, valid, expired, malformed, {"Authorization": "Basic x"})

    feature = _square(28, 28)
    singles = [
        await oidc_client.post("/items", json=feature, headers=headers)
        for headers in credential_variants
    ]
    assert {response.status_code for response in singles} == {201}
    assert all(response.content == singles[0].content for response in singles)
    assert all(
        response.headers["Location"] == singles[0].headers["Location"] for response in singles
    )
    assert all(
        response.headers["content-type"] == singles[0].headers["content-type"]
        for response in singles
    )

    feature_collection = {"type": "FeatureCollection", "features": [_square(29, 29)]}
    bulks = [
        await oidc_client.post("/items/bulk", json=feature_collection, headers=headers)
        for headers in credential_variants
    ]
    assert {response.status_code for response in bulks} == {200}
    assert all(response.content == bulks[0].content for response in bulks)
    assert all(
        response.headers["content-type"] == bulks[0].headers["content-type"] for response in bulks
    )

    resolver_path = f"/{singles[0].json()['geoid']}"
    resolutions = [
        await oidc_client.get(resolver_path, headers=headers) for headers in credential_variants
    ]
    assert {response.status_code for response in resolutions} == {200}
    assert all(response.content == resolutions[0].content for response in resolutions)
    assert all(
        response.headers["content-type"] == resolutions[0].headers["content-type"]
        for response in resolutions
    )
    assert set(resolutions[0].json()["properties"]) == {"geoid", "uri"}


@pytest.mark.parametrize(
    "path,body",
    [
        ("/items", _square(30, 30)),
        ("/collections/public/items", _square(31, 31)),
        (
            "/items/bulk",
            {"type": "FeatureCollection", "features": [_square(32, 32)]},
        ),
        (
            "/collections/public/items/bulk",
            {"type": "FeatureCollection", "features": [_square(33, 33)]},
        ),
    ],
)
async def test_public_collection_mints_record_anonymous_provenance(
    oidc_client, make_token, bearer, session, path, body
):
    from geoid.repositories import place_repo

    headers = bearer(make_token(sub="must-not-be-recorded"))
    response = await oidc_client.post(path, headers=headers, json=body)
    assert response.status_code in {200, 201}, response.text
    geoid = (
        response.json()["geoid"]
        if response.status_code == 201
        else response.json()["accepted"][0]["geoid"]
    )

    row = await place_repo.get_by_geoid(session, geoid)
    assert row is not None
    assert row["provenance"]["created_by"] is None


# --- hidden but live ----------------------------------------------------------
# Hiding is a Swagger-visibility change, never an authorization control: the
# routes behind the narrowed schema keep answering exactly as before.


async def test_hidden_routes_stay_live(client, admin_headers):
    schema_paths = set((await client.get("/openapi.json")).json()["paths"])
    assert schema_paths == {"/items", "/items/bulk", "/{geoid}"}

    assert (await client.post("/collections/public/items", json=_square(21, 21))).status_code == 201
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/conformance")).status_code == 200
    assert (await client.get("/collections", headers=admin_headers)).status_code == 200
    assert (await client.get("/me/geoids", headers=admin_headers)).status_code == 200

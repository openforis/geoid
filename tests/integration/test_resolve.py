"""Integration tests for the bulk resolver, ``POST /bulk_resolve`` (ADR-010).

A list of geoids in, one GeoJSON FeatureCollection out: each found geoid once, in
first-request order, with exactly the ``GET /{geoid}`` body; unknown ids in
``not_found``. Always 200 except a malformed body (422) or one over the cap (413).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _square(x: int, y: int) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]],
        },
    }


async def _mint(client, *features: dict) -> list[str]:
    resp = await client.post(
        "/items/bulk", json={"type": "FeatureCollection", "features": list(features)}
    )
    assert resp.status_code == 200, resp.text
    return [accepted["geoid"] for accepted in resp.json()["accepted"]]


@pytest.fixture
async def capped_client(db_clean):
    """A client whose app sees a bulk-resolve cap of 2, for exact-boundary tests."""
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, bulk_resolve_max_geoids=2
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def test_returns_each_feature_in_request_order_as_get_resolves_it(client):
    first, second, third = await _mint(client, _square(0, 0), _square(5, 5), _square(10, 10))

    resp = await client.post("/bulk_resolve", json={"geoids": [third, first, second]})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/geo+json"
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert body["not_found"] == []
    assert [feature["id"] for feature in body["features"]] == [third, first, second]
    for feature in body["features"]:
        assert feature == (await client.get(f"/{feature['id']}")).json()


async def test_duplicate_geoids_are_returned_once(client):
    (geoid,) = await _mint(client, _square(0, 0))
    missing = str(uuid.uuid4())

    resp = await client.post("/bulk_resolve", json={"geoids": [geoid, missing, geoid, missing]})

    body = resp.json()
    assert [feature["id"] for feature in body["features"]] == [geoid]
    assert body["not_found"] == [missing]


async def test_unknown_geoids_are_listed_in_not_found(client):
    (geoid,) = await _mint(client, _square(0, 0))
    missing = str(uuid.uuid4())

    resp = await client.post("/bulk_resolve", json={"geoids": [missing, geoid]})

    assert resp.status_code == 200
    body = resp.json()
    assert [feature["id"] for feature in body["features"]] == [geoid]
    assert body["not_found"] == [missing]


async def test_all_unknown_geoids_answer_200_with_no_features(client):
    missing = [str(uuid.uuid4()), str(uuid.uuid4())]

    resp = await client.post("/bulk_resolve", json={"geoids": missing})

    assert resp.status_code == 200
    assert resp.json() == {"type": "FeatureCollection", "features": [], "not_found": missing}


@pytest.mark.parametrize(
    "body",
    [
        {"geoids": ["not-a-uuid"]},
        {"geoids": []},
        {},
    ],
)
async def test_malformed_body_is_422(client, body):
    resp = await client.post("/bulk_resolve", json=body)
    assert resp.status_code == 422


async def test_exactly_at_cap_succeeds_and_one_over_is_413(capped_client):
    at_cap = await capped_client.post(
        "/bulk_resolve", json={"geoids": [str(uuid.uuid4()), str(uuid.uuid4())]}
    )
    assert at_cap.status_code == 200

    over = await capped_client.post("/bulk_resolve", json={"geoids": [str(uuid.uuid4())] * 3})
    assert over.status_code == 413
    body = over.json()
    assert body["count"] == 3
    assert body["limit"] == 2
    assert "GEOID_BULK_RESOLVE_MAX_GEOIDS" in body["message"]

"""Integration: collections with the inert ``public_read=false`` field end to end.

Existence is never masked on either resolver (client ruling 2026-07-09 round 2).
The public geoid resolver is authentication-invariant and always returns the
geometry-only body; the hidden external-id resolver remains full for members and
masked for everyone else. 404 is reserved for a genuinely unknown id. Also covers
the idempotent mint's uniform body across a private incumbent (single + bulk),
``GET /me/geoids``, and the sysadmin ``/manage`` item inventory.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# Module helpers close over ADMIN; the autouse fixture repopulates it per test
# with a fresh sysadmin JWT (there is no static credential to inline anymore).
ADMIN: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _admin_credential(admin_headers):
    ADMIN.clear()
    ADMIN.update(admin_headers)


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


async def _create(client, slug: str, **over) -> None:
    body = {"id": slug, "public_write": False, **over}
    resp = await client.post("/manage/collections", headers=ADMIN, json=body)
    assert resp.status_code == 201, resp.text


async def _grant(client, slug: str, email: str, role: str) -> None:
    resp = await client.post(
        f"/collections/{slug}/grants", headers=ADMIN, json={"email": email, "role": role}
    )
    assert resp.status_code == 201, resp.text


async def _mint(client, slug: str, feature: dict, headers: dict | None = None) -> dict:
    resp = await client.post(f"/collections/{slug}/items", headers=headers or ADMIN, json=feature)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- public_read default --------------------------------------------------------


async def test_public_read_defaults_true(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "plain"})
    listed = (await client.get("/manage/collections", headers=admin_headers)).json()
    by_id = {c["id"]: c for c in listed}
    # Both a freshly created collection and the bootstrap `public` stay readable.
    assert by_id["plain"]["public_read"] is True
    assert by_id["public"]["public_read"] is True


# --- resolver existence: open on both; public body is always masked ----------------


async def test_resolver_serves_private_collection_to_any_geoid_holder(
    oidc_client, make_token, bearer
):
    # The geoid is the capability: exact-geoid resolution ignores public_read.
    await _create(oidc_client, "priv", public_read=False)
    minted = await _mint(oidc_client, "priv", _square(10, 10))
    geoid = minted["geoid"]

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    for headers in (None, stranger, ADMIN):
        resp = await oidc_client.get(f"/{geoid}", headers=headers)
        assert resp.status_code == 200, resp.text
        feature = resp.json()
        assert feature["type"] == "Feature"
        assert feature["id"] == geoid


async def test_external_id_resolver_serves_private_collection_masked(
    oidc_client, make_token, bearer
):
    # Consistent with GET /{geoid}: an existing (collection, external_id) answers
    # 200 to every caller — only the BODY is caller-aware. 404 is reserved for a
    # genuinely unknown external_id, for members and non-members alike.
    await _create(oidc_client, "priv-x", public_read=False)
    await _grant(oidc_client, "priv-x", "viewer@x.org", "viewer")
    await _mint(oidc_client, "priv-x", _square(20, 20, external_id="ext-1"))

    url = "/collections/priv-x/external/ext-1"
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    for headers in (None, stranger):
        resp = await oidc_client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["properties"]) == {"geoid", "uri"}

    for headers in (viewer, ADMIN):
        resp = await oidc_client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["properties"]["external_id"] == "ext-1"

    for headers in (None, stranger, viewer, ADMIN):
        unknown = await oidc_client.get("/collections/priv-x/external/nope", headers=headers)
        assert unknown.status_code == 404
        assert unknown.json()["message"] == "place not found: priv-x/nope"


async def test_public_collection_still_resolves_anonymously(client, unit_square_ccw):
    minted = await _mint(client, "public", unit_square_ccw, headers={})
    assert (await client.get(f"/{minted['geoid']}")).status_code == 200


async def test_geoid_resolver_ignores_malformed_bearer(client, unit_square_ccw):
    minted = await _mint(client, "public", unit_square_ccw)
    resp = await client.get(f"/{minted['geoid']}", headers={"Authorization": "Bearer nope"})
    anonymous = await client.get(f"/{minted['geoid']}")
    assert resp.status_code == anonymous.status_code == 200
    assert resp.content == anonymous.content


async def test_external_id_resolver_rejects_malformed_bearer(client, ext_collection):
    # The external-id resolver keeps require_principal: a PRESENT-but-invalid
    # credential is 401, never silently anonymous. (A managed collection — the
    # public one no longer resolves by external_id at all.)
    await _mint(client, ext_collection, _square(15, 15, external_id="mb-1"))
    resp = await client.get(
        f"/collections/{ext_collection}/external/mb-1", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


# --- public geoid resolver masking is identity- and public_read-independent --------


async def test_geoid_resolver_masks_metadata_for_every_caller(oidc_client, make_token, bearer):
    creator = bearer(make_token(sub="kc-cr", email="cr@x.org"))
    minted = await _mint(
        oidc_client, "public", _square(85, 40, external_id="mask-1"), headers=creator
    )
    geoid = minted["geoid"]

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    for headers in (None, stranger, creator, ADMIN):
        resp = await oidc_client.get(f"/{geoid}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == geoid
        assert body["geometry"]["type"] == "Polygon"
        assert set(body["properties"]) == {"geoid", "uri"}
        assert {link["rel"] for link in body["links"]} == {"self", "alternate"}


async def test_viewer_grant_does_not_change_public_geoid_body(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-m", public_read=False)
    await _grant(oidc_client, "priv-m", "viewer@x.org", "viewer")
    minted = await _mint(oidc_client, "priv-m", _square(105, 40, external_id="mask-3"))

    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    body = (await oidc_client.get(f"/{minted['geoid']}", headers=viewer)).json()
    assert set(body["properties"]) == {"geoid", "uri"}

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    masked = (await oidc_client.get(f"/{minted['geoid']}", headers=stranger)).json()
    assert set(masked["properties"]) == {"geoid", "uri"}


# --- cross-collection repeats reveal nothing about the incumbent ------------------
# A repeat mint answers 201 with the existing geoid, in a body that names no collection. That
# uniform body is what dissolves the probe oracle — a stranger POSTing a guessed
# geometry can no longer learn where, or whether, it already lives.


async def test_repeat_of_a_private_incumbent_mints_201_and_names_no_collection(
    oidc_client, make_token, bearer
):
    await _create(oidc_client, "priv-d", public_read=False)
    minted = await _mint(oidc_client, "priv-d", _square(30, 30))

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    for headers in (stranger, None):
        resp = await oidc_client.post(
            "/collections/public/items", headers=headers, json=_square(30, 30)
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body == {"geoid": minted["geoid"], "uri": minted["uri"], "external_id": None}
        assert resp.headers["Location"] == minted["uri"]


async def test_repeat_body_is_identical_for_stranger_member_and_anonymous(
    oidc_client, make_token, bearer
):
    # The oracle regression pin: membership must make NO difference to the body.
    await _create(oidc_client, "priv-e", public_read=False)
    await _grant(oidc_client, "priv-e", "viewer@x.org", "viewer")
    await _mint(oidc_client, "priv-e", _square(40, 40))

    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    bodies = []
    for headers in (viewer, stranger, ADMIN, None):
        resp = await oidc_client.post(
            "/collections/public/items", headers=headers, json=_square(40, 40)
        )
        assert resp.status_code == 201
        bodies.append(resp.json())
    assert all(body == bodies[0] for body in bodies)


async def test_bulk_repeat_of_a_private_incumbent_is_accepted(oidc_client, make_token, bearer):
    await _create(oidc_client, "priv-b", public_read=False)
    minted = await _mint(oidc_client, "priv-b", _square(70, 70))

    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))
    fc = {"type": "FeatureCollection", "features": [_square(70, 70), _square(80, 80)]}
    report = (
        await oidc_client.post("/collections/public/items/bulk", headers=stranger, json=fc)
    ).json()
    assert report["summary"] == {"received": 2, "accepted": 2, "rejected": 0}
    assert report["accepted"][0]["geoid"] == minted["geoid"]
    assert "collection" not in report["accepted"][0]


# --- GET /me/geoids ---------------------------------------------------------------


async def test_me_geoids_requires_authentication(client):
    assert (await client.get("/me/geoids")).status_code == 401


async def test_me_geoids_lists_only_own_mints_newest_first(oidc_client, make_token, bearer):
    alice = bearer(make_token(sub="kc-alice", email="alice@x.org"))
    bob = bearer(make_token(sub="kc-bob", email="bob@x.org"))
    await _create(oidc_client, "authored", public_write=True)
    first = await _mint(oidc_client, "authored", _square(100, 10), headers=alice)
    second = await _mint(
        oidc_client, "authored", _square(102, 10, external_id="mine"), headers=alice
    )
    await _mint(oidc_client, "authored", _square(104, 10), headers=bob)

    resp = await oidc_client.get("/me/geoids", headers=alice)
    assert resp.status_code == 200
    records = resp.json()
    assert [r["geoid"] for r in records] == [second["geoid"], first["geoid"]]
    assert all(
        set(r) == {"geoid", "uri", "collection", "external_id", "created_at"} for r in records
    )
    assert records[0]["external_id"] == "mine"
    assert records[0]["collection"] == "authored"

    bobs = (await oidc_client.get("/me/geoids", headers=bob)).json()
    assert len(bobs) == 1


async def test_me_geoids_pagination(oidc_client, make_token, bearer):
    carol = bearer(make_token(sub="kc-carol", email="carol@x.org"))
    await _create(oidc_client, "carol-authored", public_write=True)
    minted = {
        (await _mint(oidc_client, "carol-authored", _square(110 + 2 * i, 20), headers=carol))[
            "geoid"
        ]
        for i in range(3)
    }
    page1 = (await oidc_client.get("/me/geoids?limit=2", headers=carol)).json()
    page2 = (await oidc_client.get("/me/geoids?limit=2&offset=2", headers=carol)).json()
    assert len(page1) == 2 and len(page2) == 1
    assert {r["geoid"] for r in page1 + page2} == minted
    assert (await oidc_client.get("/me/geoids?limit=0", headers=carol)).status_code == 422


# --- sysadmin /manage item inventory ------------------------------------------------


async def test_manage_items_auth_and_404(oidc_client, make_token, bearer):
    await _create(oidc_client, "inv")
    assert (await oidc_client.get("/manage/collections/inv/items")).status_code == 401
    user = bearer(make_token(sub="kc-u", email="u@x.org"))
    assert (await oidc_client.get("/manage/collections/inv/items", headers=user)).status_code == 403
    assert (
        await oidc_client.get("/manage/collections/nope/items", headers=ADMIN)
    ).status_code == 404


async def test_manage_items_lists_collection_inventory(client, admin_headers):
    await _create(client, "inv2", public_read=False)
    first = await _mint(client, "inv2", _square(120, 30))
    second = await _mint(client, "inv2", _square(122, 30, external_id="b"))

    records = (await client.get("/manage/collections/inv2/items", headers=admin_headers)).json()
    assert [r["geoid"] for r in records] == [second["geoid"], first["geoid"]]
    assert all(
        set(r) == {"geoid", "uri", "collection", "external_id", "created_at"} for r in records
    )
    assert all(r["collection"] == "inv2" for r in records)

    page = (
        await client.get("/manage/collections/inv2/items?limit=1", headers=admin_headers)
    ).json()
    assert len(page) == 1


async def test_public_items_path_is_still_405(client):
    # The public enumeration surface stays removed: POST owns /items, GET answers 405.
    assert (await client.get("/collections/public/items")).status_code == 405

"""The authorization matrix — persona × collection-config × surface (executable spec).

Personas: anonymous, stranger (authenticated, no grant), viewer, editor, owner,
creator (minted the seed feature; holds NO grant at assert time — provenance
``created_by`` is their only link), sysadmin.

Collection configs vary both compatibility ``public_read`` and active
``public_write`` flags. The matrix proves that ``public_read`` is inert while
``public_write`` alone controls anonymous minting.

The rules this file pins (client meeting 2026-07-07 + rulings 2026-07-09, round 2):
- WRITE ladder: editor/owner/sysadmin always; everyone else iff ``public_write``.
- Repeat mints: identical 201 body for every persona and config (the mint is
  idempotent and names no collection — nothing to disclose).
- BOTH resolvers answer 200 to every caller for an existing feature (no 404
  existence mask anywhere). The public geoid resolver is authentication-invariant
  and always masked; the hidden external-id resolver remains full only for
  members/creator/sysadmin. 404 is reserved for a genuinely unknown id.
- ``/me/geoids`` is authenticated and own-only.
- ``/manage`` + ``GET /collections[/{id}]`` are sysadmin-only (401 anonymous,
  403 for everyone else — a collection's own owner included).
- Grants: only sysadmin grants the owner role; landing + conformance stay public.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# Module helpers close over ADMIN; the autouse fixture repopulates it per test
# with a fresh sysadmin JWT.
ADMIN: dict[str, str] = {}

CONFIGS = {
    "publicish": {"public_read": True, "public_write": True},
    "open-read": {"public_read": True, "public_write": False},
    "dropbox": {"public_read": False, "public_write": True},
    "vault": {"public_read": False, "public_write": False},
}

GRANTEES = ("viewer", "editor", "owner")
# Personas whose write is grant-carried (allowed regardless of public_write).
WRITERS_ALWAYS = {"editor", "owner", "sysadmin"}
# Personas who see the full feature body.
MEMBERS = {"viewer", "editor", "owner", "creator", "sysadmin"}


@pytest.fixture(autouse=True)
def _admin_credential(admin_headers):
    ADMIN.clear()
    ADMIN.update(admin_headers)


@pytest.fixture
def personas(make_token, bearer):
    """headers per persona; sysadmin comes from ADMIN, anonymous is None."""
    named = {
        "stranger": ("kc-st", "stranger@x.org"),
        "viewer": ("kc-vw", "viewer@x.org"),
        "editor": ("kc-ed", "editor@x.org"),
        "owner": ("kc-ow", "owner@x.org"),
        "creator": ("kc-cr", "creator@x.org"),
    }
    headers = {
        name: bearer(make_token(sub=sub, email=email)) for name, (sub, email) in named.items()
    }
    headers["anonymous"] = None
    headers["sysadmin"] = ADMIN
    return headers


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


async def _setup_collection(client, slug: str, cfg: dict, personas: dict) -> dict:
    """Create the collection, grant the ladder, and seed the creator's mint.

    The creator mints via a TEMPORARY editor grant that is revoked immediately —
    at assert time their only link to the feature is provenance ``created_by``.
    Returns the seed mint body ({geoid, uri, collection, external_id}).
    """
    await _create(client, slug, **cfg)
    for role in GRANTEES:
        await _grant(client, slug, f"{role}@x.org", role)
    await _grant(client, slug, "creator@x.org", "editor")
    seed = await _mint(
        client, slug, _square(0, 0, external_id=f"seed-{slug}"), headers=personas["creator"]
    )
    revoked = await client.delete(f"/collections/{slug}/grants/creator@x.org", headers=ADMIN)
    assert revoked.status_code == 204, revoked.text
    return seed


def _assert_masked(feature: dict) -> None:
    """Ruling 4: the masked body is the bare minimum — geometry + {geoid, uri}."""
    assert set(feature["properties"]) == {"geoid", "uri"}
    assert {link["rel"] for link in feature["links"]} == {"self", "alternate"}
    assert feature["geometry"] is not None


def _assert_full(feature: dict, *, external_id: str) -> None:
    assert feature["properties"]["external_id"] == external_id
    assert "_geoid_provenance" in feature["properties"]
    assert "created_at" in feature["properties"]
    assert "collection" in {link["rel"] for link in feature["links"]}


# --- write ladder: single mint + bulk ----------------------------------------------


@pytest.mark.parametrize("config", CONFIGS)
async def test_mint_matrix(oidc_client, personas, config):
    cfg = CONFIGS[config]
    slug = f"mint-{config}"
    await _setup_collection(oidc_client, slug, cfg, personas)

    for index, (persona, headers) in enumerate(personas.items()):
        allowed = persona in WRITERS_ALWAYS or cfg["public_write"]
        resp = await oidc_client.post(
            f"/collections/{slug}/items", headers=headers, json=_square(10 * (index + 1), 0)
        )
        expected = 201 if allowed else 403
        assert resp.status_code == expected, f"{persona} on {config}: {resp.text}"


@pytest.mark.parametrize("config", CONFIGS)
async def test_bulk_matrix(oidc_client, personas, config):
    cfg = CONFIGS[config]
    slug = f"bulk-{config}"
    await _setup_collection(oidc_client, slug, cfg, personas)

    for index, (persona, headers) in enumerate(personas.items()):
        allowed = persona in WRITERS_ALWAYS or cfg["public_write"]
        fc = {"type": "FeatureCollection", "features": [_square(10 * (index + 1), 20)]}
        resp = await oidc_client.post(f"/collections/{slug}/items/bulk", headers=headers, json=fc)
        expected = 200 if allowed else 403
        assert resp.status_code == expected, f"{persona} on {config}: {resp.text}"


# --- repeat mints answer identically for every persona ------------------------------


@pytest.mark.parametrize("config", CONFIGS)
async def test_repeat_mint_matrix(oidc_client, personas, config):
    cfg = CONFIGS[config]
    slug = f"dup-{config}"
    seed = await _setup_collection(oidc_client, slug, cfg, personas)
    # A separate open-write target so EVERY persona (anonymous included) can
    # re-submit the seed's geometry.
    await _create(oidc_client, "target", public_write=True)

    expected = {"geoid": seed["geoid"], "uri": seed["uri"], "external_id": None}
    for persona, headers in personas.items():
        resp = await oidc_client.post(
            "/collections/target/items", headers=headers, json=_square(0, 0)
        )
        assert resp.status_code == 201, f"{persona} on {config}: {resp.text}"
        # Persona- and config-independent: the body is the incumbent geoid and
        # nothing else. Membership buys no extra field, so a repeat POST is not a
        # probe for where a geometry lives.
        assert resp.json() == expected, f"{persona} on {config} diverged"


# --- GET /{geoid}: authentication-invariant masked representation ------------------


@pytest.mark.parametrize("config", CONFIGS)
async def test_resolver_body_matrix(oidc_client, personas, config):
    cfg = CONFIGS[config]
    slug = f"res-{config}"
    seed = await _setup_collection(oidc_client, slug, cfg, personas)

    for persona, headers in personas.items():
        resp = await oidc_client.get(f"/{seed['geoid']}", headers=headers)
        assert resp.status_code == 200, f"{persona} on {config}: {resp.text}"
        _assert_masked(resp.json())


# --- external-id resolver: open existence (like /{geoid}); body follows membership --


@pytest.mark.parametrize("config", CONFIGS)
async def test_external_id_matrix(oidc_client, personas, config):
    cfg = CONFIGS[config]
    slug = f"ext-{config}"
    await _setup_collection(oidc_client, slug, cfg, personas)
    url = f"/collections/{slug}/external/seed-{slug}"

    for persona, headers in personas.items():
        # Existence is never masked (mirrors GET /{geoid}); only the body is
        # caller-aware. The creator's full body rides created_by == sub alone,
        # on EVERY config — the old private-collection 404 is gone.
        resp = await oidc_client.get(url, headers=headers)
        assert resp.status_code == 200, f"{persona} on {config}: {resp.text}"
        feature = resp.json()
        if persona in MEMBERS:
            _assert_full(feature, external_id=f"seed-{slug}")
        else:
            _assert_masked(feature)
        unknown = await oidc_client.get(f"/collections/{slug}/external/nope", headers=headers)
        assert unknown.status_code == 404, f"{persona} on {config}: {unknown.text}"


# --- /me/geoids: authenticated, own-only --------------------------------------------


async def test_me_geoids_is_authenticated_and_own_only(oidc_client, personas):
    seed = await _setup_collection(oidc_client, "mine", CONFIGS["publicish"], personas)

    assert (await oidc_client.get("/me/geoids")).status_code == 401

    creators = (await oidc_client.get("/me/geoids", headers=personas["creator"])).json()
    assert [r["geoid"] for r in creators] == [seed["geoid"]]

    strangers = (await oidc_client.get("/me/geoids", headers=personas["stranger"])).json()
    assert strangers == []


# --- admin-only surfaces: /manage + collection listing/describe ---------------------


async def test_admin_surfaces_are_sysadmin_only(oidc_client, personas):
    await _setup_collection(oidc_client, "adm", CONFIGS["publicish"], personas)

    surfaces = (
        ("GET", "/manage/collections"),
        ("GET", "/manage/collections/adm/items"),
        ("GET", "/collections"),
        ("GET", "/collections/adm"),
    )
    for method, path in surfaces:
        assert (await oidc_client.request(method, path)).status_code == 401, path
        # Even the collection's own owner is 403 here (stricter than the spec).
        for persona in ("stranger", "owner"):
            resp = await oidc_client.request(method, path, headers=personas[persona])
            assert resp.status_code == 403, f"{persona} {path}: {resp.text}"
        assert (await oidc_client.request(method, path, headers=ADMIN)).status_code == 200, path

    create = {"id": "adm2", "public_write": False}
    assert (await oidc_client.post("/manage/collections", json=create)).status_code == 401
    for persona in ("stranger", "owner"):
        resp = await oidc_client.post("/manage/collections", headers=personas[persona], json=create)
        assert resp.status_code == 403, persona
    assert (
        await oidc_client.post("/manage/collections", headers=ADMIN, json=create)
    ).status_code == 201


# --- grants: manage rights + the sysadmin-only owner role ---------------------------


async def test_grant_rules(oidc_client, personas):
    await _setup_collection(oidc_client, "gr", CONFIGS["publicish"], personas)
    url = "/collections/gr/grants"
    body = {"email": "new@x.org", "role": "viewer"}

    assert (await oidc_client.post(url, json=body)).status_code == 401
    for persona in ("stranger", "viewer", "editor"):
        resp = await oidc_client.post(url, headers=personas[persona], json=body)
        assert resp.status_code == 403, persona

    # The owner staffs editors/viewers but cannot mint peer owners; sysadmin can.
    assert (await oidc_client.post(url, headers=personas["owner"], json=body)).status_code == 201
    owner_body = {"email": "peer@x.org", "role": "owner"}
    assert (
        await oidc_client.post(url, headers=personas["owner"], json=owner_body)
    ).status_code == 403
    assert (await oidc_client.post(url, headers=ADMIN, json=owner_body)).status_code == 201

    # Last-owner guard: with two owners the demotion passes; the last one is 409.
    demote_peer = {"email": "peer@x.org", "role": "editor"}
    assert (await oidc_client.post(url, headers=ADMIN, json=demote_peer)).status_code == 201
    demote_last = {"email": "owner@x.org", "role": "editor"}
    assert (await oidc_client.post(url, headers=ADMIN, json=demote_last)).status_code == 409


# --- public surfaces + auth singletons ----------------------------------------------


async def test_landing_and_conformance_stay_public(oidc_client):
    assert (await oidc_client.get("/")).status_code == 200
    assert (await oidc_client.get("/conformance")).status_code == 200


async def test_malformed_bearer_is_ignored_publicly_but_401s_on_hidden_resolver(
    oidc_client, personas
):
    seed = await _setup_collection(oidc_client, "mb", CONFIGS["publicish"], personas)
    garbage = {"Authorization": "Bearer nope"}
    public = await oidc_client.get(f"/{seed['geoid']}", headers=garbage)
    assert public.status_code == 200
    _assert_masked(public.json())
    assert (
        await oidc_client.get("/collections/mb/external/seed-mb", headers=garbage)
    ).status_code == 401


async def test_wkt_on_a_masked_read_is_the_bare_geometry(oidc_client, personas):
    # The masked body still carries the geometry, so the WKT rendering of a
    # non-member read works and leaks nothing beyond it.
    seed = await _setup_collection(oidc_client, "wkt", CONFIGS["vault"], personas)
    resp = await oidc_client.get(f"/{seed['geoid']}?f=wkt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("POLYGON")

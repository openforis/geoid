"""Integration: per-collection grant ladder (viewer < editor < owner) on the
write + manage paths, plus Keycloak-``sub`` backfill on first authorized access.

These pin who may WRITE to a collection and who may MANAGE its grants. Resolver
metadata visibility (membership-based and ``public_read``-independent) is pinned
in ``test_private_collections.py``; ``GET /collections`` stays admin-gated.
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


async def _create(client, slug: str, **over) -> None:
    body = {"id": slug, "public_write": False, **over}
    resp = await client.post("/manage/collections", headers=ADMIN, json=body)
    assert resp.status_code == 201, resp.text


async def _grant(client, slug: str, email: str, role: str) -> None:
    resp = await client.post(
        f"/collections/{slug}/grants", headers=ADMIN, json={"email": email, "role": role}
    )
    assert resp.status_code == 201, resp.text


async def test_editor_writes_viewer_and_nongrantee_cannot(
    oidc_client, make_token, bearer, unit_square_ccw, other_square
):
    await _create(oidc_client, "team")
    await _grant(oidc_client, "team", "editor@x.org", "editor")
    await _grant(oidc_client, "team", "viewer@x.org", "viewer")

    editor = bearer(make_token(sub="kc-ed", email="editor@x.org"))
    viewer = bearer(make_token(sub="kc-vw", email="viewer@x.org"))
    stranger = bearer(make_token(sub="kc-st", email="stranger@x.org"))

    assert (
        await oidc_client.post("/collections/team/items", headers=editor, json=unit_square_ccw)
    ).status_code == 201
    assert (
        await oidc_client.post("/collections/team/items", headers=viewer, json=other_square)
    ).status_code == 403
    assert (
        await oidc_client.post("/collections/team/items", headers=stranger, json=other_square)
    ).status_code == 403


async def test_sysadmin_jwt_bypasses_grants(oidc_client, make_token, bearer, unit_square_ccw):
    await _create(oidc_client, "bypass")
    admin = bearer(make_token(sub="kc-adm", email="adm@fao.org", roles=["geoid.sysadmin"]))
    assert (
        await oidc_client.post("/collections/bypass/items", headers=admin, json=unit_square_ccw)
    ).status_code == 201


async def test_owner_can_manage_grants(oidc_client, make_token, bearer):
    await _create(oidc_client, "owned")
    await _grant(oidc_client, "owned", "owner@x.org", "owner")
    owner = bearer(make_token(sub="kc-ow", email="owner@x.org"))

    add = await oidc_client.post(
        "/collections/owned/grants", headers=owner, json={"email": "new@x.org", "role": "viewer"}
    )
    assert add.status_code == 201
    listed = await oidc_client.get("/collections/owned/grants", headers=owner)
    assert listed.status_code == 200
    assert {g["email"] for g in listed.json()} >= {"owner@x.org", "new@x.org"}


async def test_editor_cannot_manage_grants(oidc_client, make_token, bearer):
    await _create(oidc_client, "team2")
    await _grant(oidc_client, "team2", "editor@x.org", "editor")
    editor = bearer(make_token(sub="kc-ed", email="editor@x.org"))
    # editor is not owner → cannot list/manage grants (403)
    assert (await oidc_client.get("/collections/team2/grants", headers=editor)).status_code == 403


async def test_grant_revoke_then_revoke_again_is_404(oidc_client):
    await _create(oidc_client, "rev")
    await _grant(oidc_client, "rev", "temp@x.org", "viewer")
    deleted = await oidc_client.delete("/collections/rev/grants/temp@x.org", headers=ADMIN)
    assert deleted.status_code == 204
    # revoking again → 404
    assert (
        await oidc_client.delete("/collections/rev/grants/temp@x.org", headers=ADMIN)
    ).status_code == 404


async def test_grant_role_upsert_changes_write_outcome(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await _create(oidc_client, "promote")
    await _grant(oidc_client, "promote", "p@x.org", "viewer")
    user = bearer(make_token(sub="kc-p", email="p@x.org"))
    # viewer can't write
    assert (
        await oidc_client.post("/collections/promote/items", headers=user, json=unit_square_ccw)
    ).status_code == 403
    # promote to editor (upsert) → can write
    await _grant(oidc_client, "promote", "p@x.org", "editor")
    assert (
        await oidc_client.post("/collections/promote/items", headers=user, json=unit_square_ccw)
    ).status_code == 201


async def test_subject_backfilled_on_first_authorized_write(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await _create(oidc_client, "bf")
    # Granted by admin with no subject known → principal_subject is null initially.
    await _grant(oidc_client, "bf", "dave@x.org", "editor")
    before = await oidc_client.get("/collections/bf/grants", headers=ADMIN)
    dave_before = next(g for g in before.json() if g["email"] == "dave@x.org")
    assert dave_before["subject"] is None

    # Dave's first authorized write backfills his Keycloak sub.
    dave = bearer(make_token(sub="kc-dave", email="dave@x.org"))
    assert (
        await oidc_client.post("/collections/bf/items", headers=dave, json=unit_square_ccw)
    ).status_code == 201

    after = await oidc_client.get("/collections/bf/grants", headers=ADMIN)
    dave_after = next(g for g in after.json() if g["email"] == "dave@x.org")
    assert dave_after["subject"] == "kc-dave"


# --- H6: the unverified-email guard is load-bearing ---------------------------


async def test_unverified_email_grant_is_not_honored(
    oidc_client, make_token, bearer, unit_square_ccw
):
    # The only defense against registering a grantee's email unverified in Keycloak
    # and inheriting the grant: email_verified=False must yield NO grant → 403.
    await _create(oidc_client, "uv")
    await _grant(oidc_client, "uv", "ed@x.org", "editor")
    unverified = bearer(make_token(sub="kc-ed", email="ed@x.org", email_verified=False))
    resp = await oidc_client.post("/collections/uv/items", headers=unverified, json=unit_square_ccw)
    assert resp.status_code == 403


async def test_token_without_email_claim_gets_no_grant(
    oidc_client, make_token, bearer, unit_square_ccw
):
    await _create(oidc_client, "noemail")
    await _grant(oidc_client, "noemail", "ed@x.org", "editor")
    no_email = bearer(make_token(sub="kc-ed"))  # no email/email_verified claims at all
    resp = await oidc_client.post(
        "/collections/noemail/items", headers=no_email, json=unit_square_ccw
    )
    assert resp.status_code == 403


# --- H7: cross-collection grant isolation --------------------------------------


async def test_grant_on_one_collection_confers_nothing_on_another(
    oidc_client, make_token, bearer, unit_square_ccw, other_square
):
    # If get_grant ever dropped its collection_id predicate, an owner anywhere could
    # write/manage everywhere — this pins the isolation with a real cross-collection probe.
    await _create(oidc_client, "col-a")
    await _create(oidc_client, "col-b")
    await _grant(oidc_client, "col-a", "owner-a@x.org", "owner")
    owner_a = bearer(make_token(sub="kc-oa", email="owner-a@x.org"))

    # Sanity: the credential is live on its own collection.
    ok = await oidc_client.post("/collections/col-a/items", headers=owner_a, json=unit_square_ccw)
    assert ok.status_code == 201
    # Writing into B → 403.
    assert (
        await oidc_client.post("/collections/col-b/items", headers=owner_a, json=other_square)
    ).status_code == 403
    # Managing B's grants → 403 (list and create).
    assert (await oidc_client.get("/collections/col-b/grants", headers=owner_a)).status_code == 403
    assert (
        await oidc_client.post(
            "/collections/col-b/grants",
            headers=owner_a,
            json={"email": "x@x.org", "role": "editor"},
        )
    ).status_code == 403


# --- M1: a recorded-sub mismatch denies the grant ------------------------------


async def test_sub_mismatch_denies_the_grant(
    oidc_client, make_token, bearer, unit_square_ccw, other_square
):
    await _create(oidc_client, "subm")
    await _grant(oidc_client, "subm", "eve@x.org", "editor")
    original = bearer(make_token(sub="kc-original", email="eve@x.org"))
    # First authorized write backfills principal_subject = kc-original.
    assert (
        await oidc_client.post("/collections/subm/items", headers=original, json=unit_square_ccw)
    ).status_code == 201
    # Same (reused) email under a DIFFERENT Keycloak identity → grant not honored.
    imposter = bearer(make_token(sub="kc-imposter", email="eve@x.org"))
    assert (
        await oidc_client.post("/collections/subm/items", headers=imposter, json=other_square)
    ).status_code == 403
    # The recorded identity keeps working.
    assert (
        await oidc_client.post("/collections/subm/items", headers=original, json=other_square)
    ).status_code == 201


# --- M2: last-owner guard -------------------------------------------------------


async def test_cannot_revoke_the_last_owner(oidc_client):
    await _create(oidc_client, "lo")
    await _grant(oidc_client, "lo", "solo@x.org", "owner")
    # Even the sysadmin has no bypass: grant another owner first.
    resp = await oidc_client.delete("/collections/lo/grants/solo@x.org", headers=ADMIN)
    assert resp.status_code == 409
    # With a second owner in place the revoke goes through.
    await _grant(oidc_client, "lo", "second@x.org", "owner")
    assert (
        await oidc_client.delete("/collections/lo/grants/solo@x.org", headers=ADMIN)
    ).status_code == 204


async def test_cannot_demote_the_last_owner_via_upsert(oidc_client):
    await _create(oidc_client, "lo2")
    await _grant(oidc_client, "lo2", "solo@x.org", "owner")
    demote = await oidc_client.post(
        "/collections/lo2/grants", headers=ADMIN, json={"email": "solo@x.org", "role": "editor"}
    )
    assert demote.status_code == 409
    # Re-granting owner (no demotion) is fine.
    assert (
        await oidc_client.post(
            "/collections/lo2/grants", headers=ADMIN, json={"email": "solo@x.org", "role": "owner"}
        )
    ).status_code == 201
    # A second owner unlocks the demotion.
    await _grant(oidc_client, "lo2", "second@x.org", "owner")
    assert (
        await oidc_client.post(
            "/collections/lo2/grants", headers=ADMIN, json={"email": "solo@x.org", "role": "editor"}
        )
    ).status_code == 201


# --- R6: only sysadmin may grant the owner role ----------------------------------


async def test_owner_cannot_grant_the_owner_role(oidc_client, make_token, bearer):
    await _create(oidc_client, "og")
    await _grant(oidc_client, "og", "boss@x.org", "owner")
    boss = bearer(make_token(sub="kc-boss", email="boss@x.org"))
    resp = await oidc_client.post(
        "/collections/og/grants", headers=boss, json={"email": "peer@x.org", "role": "owner"}
    )
    assert resp.status_code == 403
    assert resp.json()["collection"] == "og"


async def test_sysadmin_grants_the_owner_role(oidc_client):
    await _create(oidc_client, "og-adm")
    resp = await oidc_client.post(
        "/collections/og-adm/grants", headers=ADMIN, json={"email": "peer@x.org", "role": "owner"}
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "owner"


async def test_owner_still_grants_editor_and_viewer(oidc_client, make_token, bearer):
    await _create(oidc_client, "og-ev")
    await _grant(oidc_client, "og-ev", "boss@x.org", "owner")
    boss = bearer(make_token(sub="kc-boss", email="boss@x.org"))
    for role in ("editor", "viewer"):
        resp = await oidc_client.post(
            "/collections/og-ev/grants",
            headers=boss,
            json={"email": f"{role}@x.org", "role": role},
        )
        assert resp.status_code == 201, resp.text


async def test_owner_can_demote_another_owner_to_editor(oidc_client, make_token, bearer):
    # The gate covers GRANTING owner, not managing an existing one: a demotion's
    # target role is editor, so an owner may still apply it (spec is silent; pinned).
    await _create(oidc_client, "og-dem")
    await _grant(oidc_client, "og-dem", "boss@x.org", "owner")
    await _grant(oidc_client, "og-dem", "other@x.org", "owner")
    boss = bearer(make_token(sub="kc-boss", email="boss@x.org"))
    resp = await oidc_client.post(
        "/collections/og-dem/grants", headers=boss, json={"email": "other@x.org", "role": "editor"}
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "editor"


async def test_owner_can_revoke_another_owner(oidc_client, make_token, bearer):
    await _create(oidc_client, "og-rev")
    await _grant(oidc_client, "og-rev", "boss@x.org", "owner")
    await _grant(oidc_client, "og-rev", "other@x.org", "owner")
    boss = bearer(make_token(sub="kc-boss", email="boss@x.org"))
    assert (
        await oidc_client.delete("/collections/og-rev/grants/other@x.org", headers=boss)
    ).status_code == 204


# --- L8: anonymous on grants routes → 401 (matching require_admin's split) ------


async def test_anonymous_grant_routes_are_401(oidc_client):
    await _create(oidc_client, "anon401")
    assert (await oidc_client.get("/collections/anon401/grants")).status_code == 401
    assert (
        await oidc_client.post(
            "/collections/anon401/grants", json={"email": "a@x.org", "role": "editor"}
        )
    ).status_code == 401
    assert (await oidc_client.delete("/collections/anon401/grants/a@x.org")).status_code == 401


# --- M16(b): the bulk route honors the same grant ladder ------------------------


async def test_bulk_editor_jwt_accepted_viewer_jwt_403(
    oidc_client, make_token, bearer, unit_square_ccw, other_square
):
    await _create(oidc_client, "bulkg")
    await _grant(oidc_client, "bulkg", "ed@x.org", "editor")
    await _grant(oidc_client, "bulkg", "vw@x.org", "viewer")
    fc = {"type": "FeatureCollection", "features": [unit_square_ccw, other_square]}

    editor = bearer(make_token(sub="kc-e", email="ed@x.org"))
    ok = await oidc_client.post("/collections/bulkg/items/bulk", headers=editor, json=fc)
    assert ok.status_code == 200
    assert ok.json()["summary"]["accepted"] == 2

    viewer = bearer(make_token(sub="kc-v", email="vw@x.org"))
    assert (
        await oidc_client.post("/collections/bulkg/items/bulk", headers=viewer, json=fc)
    ).status_code == 403

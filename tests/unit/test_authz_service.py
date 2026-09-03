"""Unit truth-table for the pure authz predicates (can_write / can_see_metadata /
can_manage).

``load_caller_grant`` touches the DB and is exercised in the integration suite; here
the predicates are pure functions over (principal, collection-view, grant).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from geoid.deps import Principal
from geoid.services import authz_service

pytestmark = pytest.mark.unit


def _collection(*, public_write: bool):
    return SimpleNamespace(public_write=public_write)


def _grant(role: str):
    return SimpleNamespace(role=role)


ANON = Principal.anonymous()
ADMIN = Principal.admin()
USER = Principal(subject="kc-1", email="u@x.org", email_verified=True)

CLOSED = _collection(public_write=False)
OPEN = _collection(public_write=True)


# --- can_write ----------------------------------------------------------------


def test_sysadmin_writes_anything():
    assert authz_service.can_write(ADMIN, CLOSED, None) is True


def test_public_write_allows_anonymous_write():
    assert authz_service.can_write(ANON, OPEN, None) is True


def test_viewer_cannot_write():
    assert authz_service.can_write(USER, CLOSED, _grant("viewer")) is False


def test_editor_can_write():
    assert authz_service.can_write(USER, CLOSED, _grant("editor")) is True


def test_owner_can_write():
    assert authz_service.can_write(USER, CLOSED, _grant("owner")) is True


def test_authenticated_non_grantee_cannot_write_closed_collection():
    assert authz_service.can_write(USER, CLOSED, None) is False


def test_authenticated_non_grantee_can_write_open_collection():
    # public_write means open-to-all writes (anonymous AND authenticated).
    assert authz_service.can_write(USER, OPEN, None) is True


# --- can_see_metadata -----------------------------------------------------------
# Full-feature visibility is MEMBERSHIP-based and public_read-independent:
# sysadmin OR the feature's creator (non-null sub) OR any grant. Everyone else
# gets the geometry-only masked body.


def test_sysadmin_sees_metadata():
    assert authz_service.can_see_metadata(ADMIN, None, None) is True


def test_creator_sees_metadata():
    assert authz_service.can_see_metadata(USER, "kc-1", None) is True


def test_non_creator_without_grant_sees_nothing():
    assert authz_service.can_see_metadata(USER, "kc-someone-else", None) is False


def test_any_grant_sees_metadata():
    for role in ("viewer", "editor", "owner"):
        assert authz_service.can_see_metadata(USER, None, _grant(role)) is True


def test_anonymous_never_sees_metadata():
    assert authz_service.can_see_metadata(ANON, "kc-1", None) is False


def test_anonymous_creator_never_matches_anonymous_caller():
    # created_by None == subject None must NOT read as "creator" — the non-null
    # subject guard is load-bearing (anonymous mints record created_by=None).
    assert authz_service.can_see_metadata(ANON, None, None) is False


def test_authenticated_non_grantee_masked_when_creator_unknown():
    assert authz_service.can_see_metadata(USER, None, None) is False


# --- can_manage ---------------------------------------------------------------


def test_sysadmin_manages_anything():
    assert authz_service.can_manage(ADMIN, CLOSED, None) is True


def test_only_owner_grant_manages():
    assert authz_service.can_manage(USER, CLOSED, _grant("owner")) is True
    assert authz_service.can_manage(USER, CLOSED, _grant("editor")) is False
    assert authz_service.can_manage(USER, CLOSED, _grant("viewer")) is False
    assert authz_service.can_manage(USER, CLOSED, None) is False

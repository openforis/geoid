"""Unit tests for the per-collection role ladder."""

from __future__ import annotations

import pytest

from geoid.domain.roles import Role, role_at_least

pytestmark = pytest.mark.unit


def test_role_values_are_plain_strings():
    assert Role.VIEWER == "viewer"
    assert Role.EDITOR == "editor"
    assert Role.OWNER == "owner"


@pytest.mark.parametrize(
    ("have", "need", "expected"),
    [
        ("owner", Role.OWNER, True),
        ("owner", Role.EDITOR, True),
        ("owner", Role.VIEWER, True),
        ("editor", Role.OWNER, False),
        ("editor", Role.EDITOR, True),
        ("editor", Role.VIEWER, True),
        ("viewer", Role.EDITOR, False),
        ("viewer", Role.VIEWER, True),
        (Role.EDITOR, Role.EDITOR, True),
    ],
)
def test_role_at_least(have, need, expected):
    assert role_at_least(have, need) is expected


def test_role_at_least_none_is_never_enough():
    assert role_at_least(None, Role.VIEWER) is False


def test_role_at_least_unknown_role_is_not_enough():
    # The DB CHECK keeps junk out, but a defensive caller must not crash on one.
    assert role_at_least("superuser", Role.VIEWER) is False

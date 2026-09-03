"""Unit: startup bootstrap tolerates the concurrent cold-start create race (L11).

Two instances cold-starting together can both see the public collection missing
and race the create; the loser's IntegrityError must roll back and adopt the
winner's row, never crash startup. A genuine (non-race) IntegrityError re-raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from geoid.config import Settings
from geoid.services import bootstrap

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


def _wire(monkeypatch, *, get_by_slug, create):
    catalog = SimpleNamespace(id="cat-1")

    async def get_or_create_default(_session):
        return catalog

    monkeypatch.setattr(bootstrap.catalog_repo, "get_or_create_default", get_or_create_default)
    monkeypatch.setattr(bootstrap.collection_repo, "get_by_slug", get_by_slug)
    monkeypatch.setattr(bootstrap.collection_repo, "create", create)


async def test_create_race_loser_adopts_the_winners_row(monkeypatch):
    winner = SimpleNamespace(slug="public")
    reads = {"count": 0}

    async def get_by_slug(_session, _slug, *, catalog_id=None):
        reads["count"] += 1
        # First read: missing (both instances saw None); after the loser's
        # rollback the winner's committed row is visible.
        return None if reads["count"] == 1 else winner

    async def create(_session, **_kwargs):
        raise IntegrityError("INSERT ...", {}, Exception("duplicate key"))

    _wire(monkeypatch, get_by_slug=get_by_slug, create=create)
    session = _FakeSession()

    result = await bootstrap.ensure_public_collection(session, Settings(_env_file=None))

    assert result is winner
    assert session.rolled_back is True


async def test_non_race_integrity_error_reraises(monkeypatch):
    async def get_by_slug(_session, _slug, *, catalog_id=None):
        return None  # the row never appears — this was NOT the cold-start race

    async def create(_session, **_kwargs):
        raise IntegrityError("INSERT ...", {}, Exception("boom"))

    _wire(monkeypatch, get_by_slug=get_by_slug, create=create)

    with pytest.raises(IntegrityError):
        await bootstrap.ensure_public_collection(_FakeSession(), Settings(_env_file=None))

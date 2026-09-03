"""Unit tests for ``GET /health`` — DB connectivity probe, no Docker.

ASGITransport does not run the lifespan, so the default localhost DSN is never
dialed; the DB seam is ``geoid.api.health.get_sessionmaker`` (the module imports
the name directly, so patching ``geoid.db.get_sessionmaker`` would miss).
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from geoid import __version__

pytestmark = pytest.mark.unit


class _FakeSession:
    """Async-CM session whose ``execute`` is injected per test."""

    def __init__(self, execute):
        self._execute = execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        return await self._execute(statement)


def _fake_sessionmaker(execute):
    def sessionmaker():
        return _FakeSession(execute)

    return sessionmaker


async def _request_health(monkeypatch, execute):
    from geoid.api import health
    from geoid.main import create_app

    monkeypatch.setattr(health, "get_sessionmaker", lambda: _fake_sessionmaker(execute))
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/health")


async def test_health_ok_reports_db_up(monkeypatch):
    async def execute(statement):
        return None

    response = await _request_health(monkeypatch, execute)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "up",
        "version": __version__,
        "detail": None,
    }


async def test_health_db_down_returns_503(monkeypatch):
    async def execute(statement):
        raise OperationalError("SELECT 1", None, OSError("connection refused to localhost"))

    response = await _request_health(monkeypatch, execute)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["db"] == "down"
    assert body["detail"] == "OperationalError"
    # The error class name only — never the message, which can embed host/user/DSN.
    assert "localhost" not in response.text


async def test_health_pool_timeout_returns_503(monkeypatch):
    async def execute(statement):
        raise SQLAlchemyTimeoutError("pool checkout timed out")

    response = await _request_health(monkeypatch, execute)

    assert response.status_code == 503
    body = response.json()
    assert body["db"] == "down"
    assert body["detail"] == "TimeoutError"


async def test_health_hung_db_returns_503(monkeypatch):
    from geoid.api import health

    monkeypatch.setattr(health, "_PROBE_TIMEOUT_S", 0.05)

    async def execute(statement):
        await asyncio.sleep(1)

    response = await _request_health(monkeypatch, execute)

    assert response.status_code == 503
    body = response.json()
    assert body["db"] == "down"

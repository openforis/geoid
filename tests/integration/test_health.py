"""Integration test for ``GET /health`` against a real PostGIS container."""

from __future__ import annotations

import pytest

from geoid import __version__

pytestmark = pytest.mark.integration


async def test_health_reports_db_up(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "up"
    assert body["version"] == __version__
    assert body["detail"] is None

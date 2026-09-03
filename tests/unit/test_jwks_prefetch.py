"""Unit tests for the lifespan JWKS prefetch (perf F3) — no network.

``main._prefetch_jwks`` is exercised directly, with ``geoid.deps.get_jwks_client``
monkeypatched as a plain module attribute (the helper resolves it lazily at call
time), so the real ``lru_cache``'d getter — and the ``dependency_overrides`` /
``cache_clear()`` seams other tests rely on — are never touched.
"""

from __future__ import annotations

import logging
import time
import types
from unittest.mock import Mock

import pytest

from geoid import main

pytestmark = pytest.mark.unit


def _geoid_records(caplog):
    return [r for r in caplog.records if r.name.startswith("geoid")]


async def test_prefetch_is_noop_when_oidc_disabled(monkeypatch, caplog):
    # Arrange
    monkeypatch.setattr("geoid.deps.get_jwks_client", lambda: None)
    caplog.set_level(logging.INFO, logger="geoid")

    # Act
    await main._prefetch_jwks()

    # Assert
    assert _geoid_records(caplog) == []


async def test_prefetch_warms_key_set_once(monkeypatch, caplog):
    # Arrange
    fake_client = types.SimpleNamespace(get_jwk_set=Mock())
    monkeypatch.setattr("geoid.deps.get_jwks_client", lambda: fake_client)
    caplog.set_level(logging.INFO, logger="geoid")

    # Act
    await main._prefetch_jwks()

    # Assert
    assert fake_client.get_jwk_set.call_count == 1
    infos = [r for r in _geoid_records(caplog) if r.levelno == logging.INFO]
    assert any("signing keys warmed" in r.getMessage() for r in infos)


async def test_prefetch_swallows_fetch_errors(monkeypatch, caplog):
    # Arrange
    fake_client = types.SimpleNamespace(get_jwk_set=Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr("geoid.deps.get_jwks_client", lambda: fake_client)
    caplog.set_level(logging.INFO, logger="geoid")

    # Act
    await main._prefetch_jwks()

    # Assert
    warnings = [r for r in _geoid_records(caplog) if r.levelno == logging.WARNING]
    assert any("JWKS prefetch skipped" in r.getMessage() for r in warnings)


async def test_prefetch_abandons_a_hung_fetch(monkeypatch, caplog):
    # Arrange — a fetch that outlives the timeout must be abandoned (warned), never
    # awaited to completion: readiness would otherwise stall behind a hung IdP
    monkeypatch.setattr(main, "_JWKS_PREFETCH_TIMEOUT_S", 0.05)
    fake_client = types.SimpleNamespace(get_jwk_set=lambda: time.sleep(0.5))
    monkeypatch.setattr("geoid.deps.get_jwks_client", lambda: fake_client)
    caplog.set_level(logging.INFO, logger="geoid")

    # Act
    await main._prefetch_jwks()

    # Assert — the timeout won the race: skipped-warning, no warmed-info
    records = _geoid_records(caplog)
    assert any(
        "JWKS prefetch skipped" in r.getMessage() for r in records if r.levelno == logging.WARNING
    )
    assert not any("signing keys warmed" in r.getMessage() for r in records)


async def test_prefetch_swallows_import_error(monkeypatch, caplog):
    # Arrange — an image built without the oidc extra raises ImportError from the getter
    monkeypatch.setattr(
        "geoid.deps.get_jwks_client", Mock(side_effect=ImportError("No module named 'jwt'"))
    )
    caplog.set_level(logging.INFO, logger="geoid")

    # Act
    await main._prefetch_jwks()

    # Assert
    warnings = [r for r in _geoid_records(caplog) if r.levelno == logging.WARNING]
    assert any("JWKS prefetch skipped" in r.getMessage() for r in warnings)

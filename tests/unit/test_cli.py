"""Unit tests for the CLI dispatch + alembic.ini resolution."""

from __future__ import annotations

import sys

import pytest

from geoid import cli

pytestmark = pytest.mark.unit


def test_alembic_ini_env_override(monkeypatch):
    monkeypatch.setenv("GEOID_ALEMBIC_INI", "/custom/alembic.ini")
    assert cli._alembic_ini() == "/custom/alembic.ini"


def test_alembic_ini_resolves_to_repo_file(monkeypatch):
    monkeypatch.delenv("GEOID_ALEMBIC_INI", raising=False)
    assert cli._alembic_ini().endswith("alembic.ini")


def test_main_dispatches_web(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_web", lambda: called.setdefault("web", True))
    monkeypatch.setattr(sys, "argv", ["geoid", "web"])
    assert cli.main() == 0
    assert called["web"] is True


def test_main_defaults_to_web(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_web", lambda: called.setdefault("web", True))
    monkeypatch.setattr(sys, "argv", ["geoid"])
    assert cli.main() == 0
    assert called["web"] is True


def test_main_dispatches_migrate(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_migrate", lambda: called.setdefault("migrate", True))
    monkeypatch.setattr(sys, "argv", ["geoid", "migrate"])
    assert cli.main() == 0
    assert called["migrate"] is True


def test_main_unknown_command_returns_2(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["geoid", "bogus"])
    with caplog.at_level("ERROR", logger="geoid.cli"):
        assert cli.main() == 2
    assert "unknown command" in caplog.text

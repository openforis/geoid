"""Unit tests for scripts/bootstrap_db.py pure helpers (no DB, no subprocess)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_db", ROOT / "scripts" / "bootstrap_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["bootstrap_db"] = module
    spec.loader.exec_module(module)
    return module


bootstrap_db = _load_script_module()


# --- _redact ------------------------------------------------------------------


def test_redact_masks_url_password():
    redacted = bootstrap_db._redact("postgresql://geoid:hunter2@db.internal:5432/geoid")
    assert "hunter2" not in redacted
    assert "***" in redacted


def test_redact_masks_keyword_dsn_password():
    redacted = bootstrap_db._redact("host=db.internal user=postgres password=hunter2")
    assert "hunter2" not in redacted
    assert "password=***" in redacted


def test_redact_leaves_passwordless_dsn_readable():
    dsn = "postgresql://geoid@db.internal:5432/geoid"
    assert "db.internal" in bootstrap_db._redact(dsn)


# --- _derive_app_url ----------------------------------------------------------


def test_derive_app_url_extracts_role_db_and_keeps_asyncpg():
    url, role, db = bootstrap_db._derive_app_url(
        "postgresql+asyncpg://geoid:pw@10.0.0.5:5432/geoid_prod", None
    )
    assert (role, db) == ("geoid", "geoid_prod")
    assert url.startswith("postgresql+asyncpg://")
    assert "pw@" in url  # effective URL keeps the password for migrate/seed/verify


def test_derive_app_url_normalizes_plain_postgresql_driver():
    url, _, _ = bootstrap_db._derive_app_url("postgresql://geoid:pw@host:5432/geoid", None)
    assert url.startswith("postgresql+asyncpg://")


def test_derive_app_url_password_override_wins():
    url, _, _ = bootstrap_db._derive_app_url(
        "postgresql+asyncpg://geoid:old@host:5432/geoid", "new-password"
    )
    assert "new-password@" in url
    assert "old@" not in url


def test_derive_app_url_rejects_non_postgresql():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("mysql://geoid:pw@host:3306/geoid", None)


def test_derive_app_url_requires_role_and_database():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("postgresql://host:5432/geoid", None)  # no role
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("postgresql://geoid:pw@host:5432", None)  # no db


def test_derive_app_url_rejects_garbage():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("not a url at all", None)


# --- _psycopg_dsn -------------------------------------------------------------


def test_psycopg_dsn_strips_the_async_driver_and_keeps_credentials():
    dsn = bootstrap_db._psycopg_dsn("postgresql+asyncpg://geoid:pw@host:5432/geoid")
    assert dsn == "postgresql://geoid:pw@host:5432/geoid"

"""scripts/bootstrap_db.py against a real PostGIS — the script's only rehearsal
before it runs against the production Cloud SQL instance.

The script is exercised the way an operator runs it (a subprocess with env vars
and flags), against the shared session container's superuser as the "admin".
Each scenario uses its own role/database so the shared ``geoid`` database used
by the rest of the integration suite is never touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bootstrap_db.py"

APP_PASSWORD = "s3cret-bootstrap"


def _endpoint(pg) -> tuple[str, str]:
    return pg.get_container_host_ip(), pg.get_exposed_port(5432)


def _admin_dsn(pg) -> str:
    host, port = _endpoint(pg)
    return f"postgresql://geoid:geoid@{host}:{port}/geoid"


def _app_url(pg, role: str, db: str, password: str = APP_PASSWORD) -> str:
    host, port = _endpoint(pg)
    return f"postgresql+asyncpg://{role}:{password}@{host}:{port}/{db}"


def _run_bootstrap(
    pg, role: str, db: str, *flags: str, password: str = APP_PASSWORD
) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GEOID_")}
    env.update(
        {
            "GEOID_BOOTSTRAP_ADMIN_DSN": _admin_dsn(pg),
            "GEOID_DATABASE_URL": _app_url(pg, role, db, password),
            "GEOID_ENVIRONMENT": "development",
        }
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--yes", *flags],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _admin_execute(pg, statement: str) -> None:
    import psycopg

    with psycopg.connect(_admin_dsn(pg), autocommit=True) as conn:
        conn.execute(statement)


def _admin_scalar(pg, query: str, params=()) -> object:
    import psycopg

    with psycopg.connect(_admin_dsn(pg), autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
    return row[0] if row else None


def _alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _snapshot(pg, role: str, db: str, password: str = APP_PASSWORD) -> dict:
    """App-state snapshot, read AS THE APP ROLE (also proves its credentials)."""
    import psycopg

    host, port = _endpoint(pg)
    with psycopg.connect(f"postgresql://{role}:{password}@{host}:{port}/{db}") as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        extensions = sorted(
            name
            for (name,) in conn.execute(
                "SELECT extname FROM pg_extension WHERE extname IN ('postgis', 'pgcrypto')"
            )
        )
        triggers = conn.execute("""
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname = 'public'
        """).fetchone()[0]
        hash_fn = conn.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'geoid_geom_hash_v2'"
        ).fetchone()[0]
        recipe_stamp = conn.execute(
            "SELECT recipe_version FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        catalogs = conn.execute("SELECT count(*) FROM catalog").fetchone()[0]
        collections = conn.execute("SELECT count(*) FROM collection").fetchone()[0]
    owner = _admin_scalar(
        pg, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (db,)
    )
    return {
        "alembic_version": version,
        "extensions": extensions,
        "triggers": triggers,
        "hash_fn": hash_fn,
        "recipe_stamp": recipe_stamp,
        "catalogs": catalogs,
        "collections": collections,
        "owner": owner,
    }


def test_fresh_bootstrap_then_idempotent_rerun(_postgis):
    role, db = "geoid_app", "geoid_appdb"

    first = _run_bootstrap(_postgis, role, db)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "✓ bootstrap complete" in first.stdout
    assert "golden vectors match" in first.stdout

    state = _snapshot(_postgis, role, db)
    assert state["owner"] == role
    assert state["alembic_version"] == _alembic_head()
    assert state["extensions"] == ["pgcrypto", "postgis"]
    assert state["triggers"] >= 7
    assert state["hash_fn"] == 1
    assert state["recipe_stamp"] == "v2"
    assert state["catalogs"] >= 1 and state["collections"] >= 1

    rerun = _run_bootstrap(_postgis, role, db)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "password untouched" in rerun.stdout
    assert _snapshot(_postgis, role, db) == state


def test_rerun_never_silently_rotates_the_password(_postgis):
    role, db = "geoid_pwcheck", "geoid_pwcheckdb"
    assert _run_bootstrap(_postgis, role, db).returncode == 0

    # A re-run with a different password in the URL must NOT rotate the role's
    # password; it fails at the verify step (credential check) with the hint.
    wrong = _run_bootstrap(
        _postgis, role, db, "--skip-migrate", "--skip-seed", password="some-new-password"
    )
    assert wrong.returncode == 1, wrong.stdout + wrong.stderr
    assert "--reset-app-password" in wrong.stdout
    assert _snapshot(_postgis, role, db)["owner"] == role  # original password still valid

    # Opting in rotates it, and the new credentials pass verification.
    reset = _run_bootstrap(
        _postgis,
        role,
        db,
        "--reset-app-password",
        "--skip-migrate",
        "--skip-seed",
        password="some-new-password",
    )
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert _snapshot(_postgis, role, db, password="some-new-password")["owner"] == role


def test_wrong_owner_database_is_refused(_postgis):
    role, db = "geoid_app2", "geoid_wrongowner"
    _admin_execute(_postgis, f"CREATE DATABASE {db} OWNER geoid")

    result = _run_bootstrap(_postgis, role, db)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "owned by" in result.stdout and "refusing" in result.stdout
    assert "ALTER DATABASE" in result.stdout  # remediation instructions printed
    owner = _admin_scalar(
        _postgis, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (db,)
    )
    assert owner == "geoid"  # untouched — the script must not auto-adopt


def test_dry_run_mutates_nothing(_postgis):
    role, db = "geoid_dry", "geoid_drydb"

    result = _run_bootstrap(_postgis, role, db, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[dry-run]" in result.stdout
    assert "nothing mutated" in result.stdout

    role_exists = _admin_scalar(
        _postgis, "SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,)
    )
    db_exists = _admin_scalar(
        _postgis, "SELECT count(*) FROM pg_database WHERE datname = %s", (db,)
    )
    assert (role_exists, db_exists) == (0, 0)

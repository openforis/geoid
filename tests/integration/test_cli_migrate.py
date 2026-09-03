"""`geoid migrate` as the Cloud Run job runs it: a subprocess whose environment
carries only the database URL — no OIDC config, GEOID_ENVIRONMENT=review."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def test_migrate_succeeds_without_oidc_config_in_review_environment(_migrated):
    # Strip all GEOID_* (conftest exports the OIDC env session-wide), then
    # provide exactly what the Cloud Run migrate job mounts.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GEOID_")}
    env["GEOID_DATABASE_URL"] = _migrated
    env["GEOID_ENVIRONMENT"] = "review"

    # cwd=ROOT: alembic.ini paths are cwd-relative. Caveat: a local .env with
    # OIDC config can mask the pre-fix red here; CI has no .env.
    result = subprocess.run(
        [sys.executable, "-m", "geoid.cli", "migrate"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr

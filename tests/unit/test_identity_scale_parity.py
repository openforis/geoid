"""Parity pin: the identity-lattice scale is ONE value in three places — the SQL
literal inside migration 0008's ``geoid_quantize_v2``, the Python reference's
``geometry_identity.SCALE``, and (as its reciprocal) the config-canonical
``GEOID_DEDUP_GRID_DEFAULT`` cell size.

The app never passes a grid/scale to the DB (v1's grid parameter is gone); the
lattice is a frozen recipe constant. This test keeps the three from drifting
apart: a retune must move ALL of them, as a migration event — under v2 an
*identity-version* event, since the geoid is derived from the lattice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from geoid.config import Settings
from geoid.domain.geometry_identity import SCALE

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "versions" / "0008_identity_recipe_v2.py"

# The quantizer body: SELECT (c * 10000000.0::float8)::bigint — the ::float8 on
# the literal is load-bearing (a numeric path rounds ties away-from-zero), so the
# pattern requires it.
_QUANTIZE_SCALE = re.compile(r"\(c \* ([0-9.eE+]+)::float8\)::bigint")


def _migration_scale_literal() -> float:
    matches = _QUANTIZE_SCALE.findall(MIGRATION.read_text())
    assert matches, "geoid_quantize_v2 scale literal not found in migration 0008"
    assert len(set(matches)) == 1, "conflicting scale literals in migration 0008"
    return float(matches[0])


def test_migration_scale_matches_the_python_reference():
    assert _migration_scale_literal() == float(SCALE)


def test_scale_is_the_reciprocal_of_the_config_grid(monkeypatch):
    monkeypatch.delenv("GEOID_DEDUP_GRID_DEFAULT", raising=False)
    grid = Settings(_env_file=None).dedup_grid_default
    assert round(1 / grid) == SCALE

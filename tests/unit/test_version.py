"""Unit test: the package version is derived from metadata, never hand-maintained."""

from __future__ import annotations

from importlib.metadata import version

import pytest

import geoid

pytestmark = pytest.mark.unit


def test_version_is_derived_from_installed_metadata():
    # Regression guard for the /health version drift: __version__ must track the
    # installed distribution metadata (which comes from pyproject.toml), so it can
    # never lag the released tag the way the old hardcoded "0.1.0" literal did.
    assert geoid.__version__ == version("geoid")

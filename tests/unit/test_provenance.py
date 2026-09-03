"""Unit tests for provenance shaping."""

from __future__ import annotations

import pytest

from geoid.domain import provenance

pytestmark = pytest.mark.unit


def test_build_provenance_is_exactly_the_three_key_contract():
    prov = provenance.build_provenance(created_by="kc-1", originating_instance="x")
    assert prov == {
        "schema": "geoid-prov/0.2",
        "created_by": "kc-1",
        "originating_instance": "x",
    }


def test_build_provenance_anonymous_has_null_created_by():
    prov = provenance.build_provenance(created_by=None, originating_instance="fao-central")
    assert prov == {
        "schema": "geoid-prov/0.2",
        "created_by": None,
        "originating_instance": "fao-central",
    }

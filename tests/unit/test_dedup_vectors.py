"""Unit tests for scripts/dedup_vectors.py (recipe v2) — committed-fixture
well-formedness, fixture ≡ pure-Python-reference recomputation (the corpus is now
verifiable WITHOUT Docker), and the check/generate logic with a fake run_sql."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from geoid.domain.geometry_identity import DegenerateGeometryError, geom_hash_v2

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "dedup_vectors", ROOT / "scripts" / "dedup_vectors.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["dedup_vectors"] = module
    spec.loader.exec_module(module)
    return module


dedup_vectors = _load_script_module()
FIXTURE = dedup_vectors.load_fixture()
VECTORS = FIXTURE["vectors"]
BY_NAME = {vector["name"]: vector for vector in VECTORS}
HASH_VECTORS = [vector for vector in VECTORS if vector["expect"] == "hash"]
ERROR_VECTORS = [vector for vector in VECTORS if vector["expect"] == "error"]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- committed fixture well-formedness -----------------------------------------


def test_fixture_is_recipe_v2_with_provenance():
    assert FIXTURE["recipe_version"] == "v2"
    assert "pure-Python reference" in FIXTURE["generated_by"]


def test_vector_names_are_unique():
    names = [vector["name"] for vector in VECTORS]
    assert len(names) == len(set(names))


def test_hash_vectors_carry_hex_digests_and_error_vectors_none():
    for vector in HASH_VECTORS:
        assert _HEX64.match(vector["sha256"]), vector["name"]
    for vector in ERROR_VECTORS:
        assert vector["sha256"] is None, vector["name"]


def test_same_as_references_resolve():
    for vector in VECTORS:
        if vector["same_as"] is not None:
            assert vector["same_as"] in BY_NAME, vector["name"]
            assert BY_NAME[vector["same_as"]]["same_as"] is None  # one level deep


def test_corpus_coverage():
    # The v1 strict/advisory split is gone (v2 is engine-independent, everything
    # is strict); coverage floor: a healthy hash corpus + the degeneracy rejects.
    assert len(HASH_VECTORS) >= 20
    assert len(ERROR_VECTORS) >= 3
    assert any(vector.get("wkt") for vector in VECTORS)  # WKT convergence pins


def test_same_as_pairs_have_equal_pinned_digests():
    for vector in VECTORS:
        if vector["same_as"] is not None:
            assert vector["sha256"] == BY_NAME[vector["same_as"]]["sha256"], vector["name"]


def test_cell_straddle_pair_pins_distinct_digests():
    # The known caveat: an absolute lattice, not a radius — 2e-8 apart, different
    # hash. Both strict under v2 (the rounding is ours, no GEOS involved).
    assert BY_NAME["cell_straddle_low"]["sha256"] != BY_NAME["cell_straddle_high"]["sha256"]


def test_collinear_extra_vertex_has_its_own_digest():
    assert BY_NAME["collinear_extra_vertex"]["sha256"] != BY_NAME["baseline_unit_square"]["sha256"]


def test_multipoint_duplicate_members_are_not_merged():
    assert (
        BY_NAME["multipoint_duplicate_members"]["sha256"] != BY_NAME["multipoint_single"]["sha256"]
    )


# --- fixture ≡ reference recomputation (no Docker needed) -----------------------


@pytest.mark.parametrize("vector", HASH_VECTORS, ids=lambda vector: vector["name"])
def test_fixture_digest_matches_reference_recomputation(vector):
    assert geom_hash_v2(vector["geojson"]).hex() == vector["sha256"], (
        f"{vector['name']!r}: the committed corpus no longer matches the Python "
        "reference — recipe code changed (a recipe-version event, never a casual fix)"
    )


@pytest.mark.parametrize("vector", ERROR_VECTORS, ids=lambda vector: vector["name"])
def test_error_vectors_are_rejected_by_the_reference(vector):
    with pytest.raises(DegenerateGeometryError):
        geom_hash_v2(vector["geojson"])


def test_generate_reproduces_the_committed_fixture_exactly():
    # The fixture is deterministic (no timestamps): regenerating on an unchanged
    # recipe must reproduce the committed file verbatim.
    assert dedup_vectors.generate_fixture() == FIXTURE


# --- load_fixture ----------------------------------------------------------------


def test_load_fixture_missing_file_raises_step_error(tmp_path):
    with pytest.raises(dedup_vectors.StepError, match="missing"):
        dedup_vectors.load_fixture(tmp_path / "nope.json")


def test_load_fixture_wrong_recipe_version_raises_step_error(tmp_path):
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"recipe_version": "v1", "vectors": []}))
    with pytest.raises(dedup_vectors.StepError, match="recipe_version"):
        dedup_vectors.load_fixture(path)


# --- check_vectors ---------------------------------------------------------------

_POINT = {"type": "Point", "coordinates": [0, 0]}

_FAKE_FIXTURE = {
    "recipe_version": "v2",
    "vectors": [
        {
            "name": "pinned_hash",
            "geojson": _POINT,
            "wkt": None,
            "expect": "hash",
            "sha256": "aa" * 32,
            "same_as": None,
            "note": None,
        },
        {
            "name": "pinned_error",
            "geojson": _POINT,
            "wkt": None,
            "expect": "error",
            "sha256": None,
            "same_as": None,
            "note": None,
        },
    ],
}


class _FakePgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"fake pg error ({sqlstate})")
        self.sqlstate = sqlstate


def _fake_run_sql(*, digest="aa" * 32, error_sqlstate="GD001"):
    """Hash vectors answer ``digest`` on both the v2 function and the wrapper;
    the error vector raises with ``error_sqlstate`` (None = returns a digest)."""
    calls = {"n": 0}

    def run_sql(query, params=None):
        calls["n"] += 1
        if calls["n"] > 2 and error_sqlstate is not None:
            raise _FakePgError(error_sqlstate)
        return digest

    return run_sql


def test_check_vectors_all_pass():
    report = dedup_vectors.check_vectors(_fake_run_sql(), _FAKE_FIXTURE)
    assert report.passed == 2
    assert report.failures == ()


def test_check_vectors_flags_digest_drift_per_function():
    report = dedup_vectors.check_vectors(_fake_run_sql(digest="ff" * 32), _FAKE_FIXTURE)
    assert report.passed == 1  # the error vector still raises GD001
    assert [failure.name for failure in report.failures] == [
        "pinned_hash [geoid_geom_hash_v2]",
        "pinned_hash [wrapper]",
    ]
    assert report.failures[0].expected == "aa" * 32
    assert report.failures[0].actual == "ff" * 32


def test_check_vectors_flags_error_vector_with_wrong_sqlstate():
    report = dedup_vectors.check_vectors(_fake_run_sql(error_sqlstate="XX000"), _FAKE_FIXTURE)
    assert report.passed == 1
    assert [failure.name for failure in report.failures] == ["pinned_error"]
    assert "GD001" in report.failures[0].expected


def test_check_vectors_flags_error_vector_that_hashes():
    report = dedup_vectors.check_vectors(_fake_run_sql(error_sqlstate=None), _FAKE_FIXTURE)
    assert report.passed == 1
    assert [failure.name for failure in report.failures] == ["pinned_error"]
    assert report.failures[0].actual.startswith("digest ")


def test_ensure_v2_deployed_hints_at_migrate():
    with pytest.raises(dedup_vectors.StepError, match="geoid migrate"):
        dedup_vectors.ensure_v2_deployed(lambda query, params=None: 0)
    dedup_vectors.ensure_v2_deployed(lambda query, params=None: 1)  # deployed: no raise


# --- refuse_silent_regeneration ----------------------------------------------------


def test_regeneration_onto_drifted_fixture_is_refused(tmp_path):
    out = tmp_path / "fixture.json"
    drifted = {
        "recipe_version": "v2",
        "vectors": [
            {**vector, "sha256": "0" * 64 if vector["sha256"] else None} for vector in VECTORS
        ],
    }
    out.write_text(json.dumps(drifted))
    fresh = dedup_vectors.generate_fixture()
    with pytest.raises(dedup_vectors.StepError, match="RECIPE-VERSION EVENT"):
        dedup_vectors.refuse_silent_regeneration(fresh, out, force=False)
    dedup_vectors.refuse_silent_regeneration(fresh, out, force=True)  # explicit opt-in


def test_regeneration_over_identical_or_missing_fixture_is_allowed(tmp_path):
    fresh = dedup_vectors.generate_fixture()
    dedup_vectors.refuse_silent_regeneration(fresh, tmp_path / "absent.json", force=False)
    same = tmp_path / "same.json"
    same.write_text(json.dumps(fresh))
    dedup_vectors.refuse_silent_regeneration(fresh, same, force=False)


# --- CLI argument validation -------------------------------------------------------


def test_cli_requires_exactly_one_mode():
    assert dedup_vectors.main([]) == 2
    assert dedup_vectors.main(["--check", "--generate"]) == 2


def test_cli_check_requires_a_dsn(monkeypatch):
    monkeypatch.delenv("GEOID_DATABASE_URL", raising=False)
    assert dedup_vectors.main(["--check"]) == 2


def test_cli_generate_needs_no_database(tmp_path):
    out = tmp_path / "generated.json"
    assert dedup_vectors.main(["--generate", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == FIXTURE

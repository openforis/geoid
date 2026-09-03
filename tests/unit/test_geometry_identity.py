"""Unit tests for the v2 identity recipe reference (``domain/geometry_identity``).

These pin the frozen ADR-007 spec: half-even quantization on the exact IEEE
product, the canonical byte layout, structural invariances (rotation / winding /
ordering), cyclic duplicate collapse, and degeneracy rejection. The integration
suite separately pins this module byte-identical to the SQL ``geoid_geom_hash_v2``.
"""

from __future__ import annotations

import struct
import uuid

import pytest

from geoid.domain import geometry_identity as gi

pytestmark = pytest.mark.unit


def _polygon(*rings) -> dict:
    return {"type": "Polygon", "coordinates": [list(r) for r in rings]}


_SQUARE = _polygon([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]])


# --- Quantization: half-even on the exact double product ----------------------


def test_scale_is_ten_million():
    assert gi.SCALE == 10_000_000


@pytest.mark.parametrize(
    ("coord", "expected"),
    [
        (0.0, 0),
        (-0.0, 0),  # the integer lattice has no -0 by construction
        (1e-7, 1),
        (-1e-7, -1),
        (5e-8, 0),  # exact .5 tie -> even (0), not up (1)
        (-5e-8, 0),  # exact -.5 tie -> even (0), not away (-1)
        (1.5e-7, 2),  # 1.5 tie -> even (2)
        (2.5e-7, 2),  # 2.5 tie -> even (2), not up (3)
        (1e-8, 0),  # sub-cell jitter absorbed
        (20.00000004, 200_000_000),  # 0.4 cells above -> snaps down
        (20.00000006, 200_000_001),  # 0.6 cells above -> snaps up
        (180.0, 1_800_000_000),
        (-180.0, -1_800_000_000),
        (90.0, 900_000_000),
        (-90.0, -900_000_000),
    ],
)
def test_quantize_known_answers(coord, expected):
    assert gi.quantize(coord) == expected


def test_tie_products_are_exact_halves():
    # The spec's tie vectors rest on these products being exactly representable
    # after ONE correctly-rounded IEEE multiply — pin that premise itself.
    assert 5e-8 * 1e7 == 0.5
    assert 1.5e-7 * 1e7 == 1.5
    assert 2.5e-7 * 1e7 == 2.5


# --- Canonical byte layout ----------------------------------------------------


def test_point_origin_byte_layout():
    got = gi.canonical_bytes({"type": "Point", "coordinates": [0, 0]})
    assert got == b"\x02\x01" + b"\x00" * 16


def test_point_negative_coordinate_is_signed_big_endian():
    got = gi.canonical_bytes({"type": "Point", "coordinates": [1e-7, -1e-7]})
    assert got == b"\x02\x01" + struct.pack(">qq", 1, -1)


def test_unit_square_byte_layout():
    # 1 ring, 4 vertices, CCW from the lexicographically smallest vertex (0,0).
    ring = struct.pack(">i", 4) + struct.pack(
        ">8q", 0, 0, gi.SCALE, 0, gi.SCALE, gi.SCALE, 0, gi.SCALE
    )
    assert gi.canonical_bytes(_SQUARE) == b"\x02\x03" + struct.pack(">i", 1) + ring


def test_multipoint_sorted_byte_layout():
    got = gi.canonical_bytes({"type": "MultiPoint", "coordinates": [[5, 5], [0, 0]]})
    body = (
        struct.pack(">i", 2)
        + struct.pack(">qq", 0, 0)
        + struct.pack(">qq", 5 * gi.SCALE, 5 * gi.SCALE)
    )
    assert got == b"\x02\x02" + body


def test_type_tags_disambiguate_equal_bodies():
    point = gi.geom_hash_v2({"type": "Point", "coordinates": [0, 0]})
    multipoint = gi.geom_hash_v2({"type": "MultiPoint", "coordinates": [[0, 0]]})
    assert point != multipoint


# --- Structural invariances (same shape -> same bytes) -------------------------


def test_ring_start_rotation_collapses():
    rotated = _polygon([[1, 1], [0, 1], [0, 0], [1, 0], [1, 1]])
    assert gi.canonical_bytes(rotated) == gi.canonical_bytes(_SQUARE)


def test_cw_winding_collapses():
    clockwise = _polygon([[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]])
    assert gi.canonical_bytes(clockwise) == gi.canonical_bytes(_SQUARE)


def test_hole_order_collapses():
    outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    hole_a = [[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]
    hole_b = [[6, 6], [7, 6], [7, 7], [6, 7], [6, 6]]
    assert gi.canonical_bytes(_polygon(outer, hole_a, hole_b)) == gi.canonical_bytes(
        _polygon(outer, hole_b, hole_a)
    )


def test_multipolygon_part_order_collapses():
    part_a = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    part_b = [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]]
    ab = {"type": "MultiPolygon", "coordinates": [part_a, part_b]}
    ba = {"type": "MultiPolygon", "coordinates": [part_b, part_a]}
    assert gi.canonical_bytes(ab) == gi.canonical_bytes(ba)


def test_multipoint_member_order_collapses():
    ab = {"type": "MultiPoint", "coordinates": [[0, 0], [5, 5]]}
    ba = {"type": "MultiPoint", "coordinates": [[5, 5], [0, 0]]}
    assert gi.canonical_bytes(ab) == gi.canonical_bytes(ba)


def test_subgrid_jitter_collapses():
    jittered = _polygon([[1e-8, 0], [1, 0], [1, 1], [0, 1], [1e-8, 0]])
    assert gi.canonical_bytes(jittered) == gi.canonical_bytes(_SQUARE)


def test_supragrid_shift_is_its_own_identity():
    shifted = _polygon([[0.001, 0], [1, 0], [1, 1], [0, 1], [0.001, 0]])
    assert gi.canonical_bytes(shifted) != gi.canonical_bytes(_SQUARE)


def test_collinear_extra_vertex_is_never_simplified():
    # v1 rule preserved: a zero-area-contribution (but nonzero-length) midpoint
    # stays detectable — the recipe removes only coincident runs.
    with_midpoint = _polygon([[0, 0], [0.5, 0], [1, 0], [1, 1], [0, 1], [0, 0]])
    assert gi.canonical_bytes(with_midpoint) != gi.canonical_bytes(_SQUARE)


# --- Cyclic duplicate collapse -------------------------------------------------


def test_consecutive_duplicate_vertices_collapse():
    doubled = _polygon([[0, 0], [1, 0], [1, 0], [1, 1], [0, 1], [0, 0]])
    assert gi.canonical_bytes(doubled) == gi.canonical_bytes(_SQUARE)


def test_wraparound_duplicates_collapse():
    # Duplicate run straddling the closure: the wrap-around trim catches what a
    # forward-only pass would miss.
    wrapped = _polygon([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0], [0, 0]])
    assert gi.canonical_bytes(wrapped) == gi.canonical_bytes(_SQUARE)


def test_sub_cell_duplicate_vertices_collapse():
    # Two listed vertices one lattice cell apart in the input text but identical
    # after quantization collapse to one.
    near_dup = _polygon([[0, 0], [1, 0], [1.00000001, 0], [1, 1], [0, 1], [0, 0]])
    assert gi.canonical_bytes(near_dup) == gi.canonical_bytes(_SQUARE)


def test_multipoint_duplicate_members_are_kept():
    # No silent merge: two members on the same lattice point stay two members.
    single = {"type": "MultiPoint", "coordinates": [[0, 0]]}
    doubled = {"type": "MultiPoint", "coordinates": [[0, 0], [1e-8, 0]]}
    assert gi.canonical_bytes(single) != gi.canonical_bytes(doubled)


# --- Degeneracy: reject, never merge or vanish ---------------------------------


def test_sub_cell_sliver_is_rejected():
    sliver = _polygon([[0, 0], [1, 0], [1, 1e-8], [0, 1e-8], [0, 0]])
    with pytest.raises(gi.DegenerateGeometryError, match="identity precision"):
        gi.canonical_bytes(sliver)


def test_collapses_to_collinear_is_rejected():
    collinear = _polygon([[0, 0], [1, 0], [2, 1e-8], [0, 0]])
    with pytest.raises(gi.DegenerateGeometryError, match="zero lattice area"):
        gi.canonical_bytes(collinear)


def test_bowtie_zero_area_is_rejected():
    # v1 silently "repaired" (and could vanish) this via MakeValid; v2 refuses:
    # its exact integer shoelace is zero.
    bowtie = _polygon([[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]])
    with pytest.raises(gi.DegenerateGeometryError, match="zero lattice area"):
        gi.canonical_bytes(bowtie)


def test_ring_collapsing_below_three_vertices_is_rejected():
    dot = _polygon([[0, 0], [1e-8, 0], [0, 1e-8], [0, 0]])
    with pytest.raises(gi.DegenerateGeometryError, match="lattice vertices"):
        gi.canonical_bytes(dot)


def test_degenerate_hole_is_rejected():
    outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    sliver_hole = [[2, 2], [3, 2], [3, 2.00000001], [2, 2]]
    with pytest.raises(gi.DegenerateGeometryError):
        gi.canonical_bytes(_polygon(outer, sliver_hole))


def test_degenerate_error_is_a_value_error():
    # The schema boundary catches ValueError; the DB backstop discriminates the
    # subclass — both must hold.
    assert issubclass(gi.DegenerateGeometryError, ValueError)


# --- Malformed input -----------------------------------------------------------


def test_unsupported_type_is_rejected():
    with pytest.raises(ValueError, match="unsupported geometry type"):
        gi.canonical_bytes({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})


def test_empty_geometry_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        gi.canonical_bytes({"type": "MultiPoint", "coordinates": []})


def test_three_dimensional_position_is_rejected():
    with pytest.raises(ValueError, match="2D"):
        gi.canonical_bytes({"type": "Point", "coordinates": [0, 0, 5]})


# --- World extent: shoelace exceeds int64 --------------------------------------


def test_world_extent_shoelace_magnitude_is_handled_exactly():
    # 2x the world's lattice area = 1.296e19 > int64 max (~9.22e18): the spec
    # mandates exact accumulation (Python int / PG numeric), pinned here.
    world = _polygon([[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]])
    got = gi.canonical_bytes(world)
    assert got[:2] == b"\x02\x03"
    assert 2 * (360 * gi.SCALE) * (180 * gi.SCALE) > 2**63 - 1


# --- geoid derivation ----------------------------------------------------------


def test_geoid_v2_is_version_8_rfc4122_and_composed_from_the_hash():
    geoid = gi.geoid_v2(_SQUARE)
    assert geoid.version == 8
    assert geoid.variant == uuid.RFC_4122
    from geoid.domain.identifiers import geoid_from_geom_hash

    assert geoid == geoid_from_geom_hash(gi.geom_hash_v2(_SQUARE))


def test_geom_hash_v2_is_deterministic():
    assert gi.geom_hash_v2(_SQUARE) == gi.geom_hash_v2(_SQUARE)
    assert len(gi.geom_hash_v2(_SQUARE)) == 32

"""Unit tests for ingest validation (RFC 7946 + lon/lat bounds + supported types).

Supported geometry types: Point, MultiPoint, Polygon, MultiPolygon. Lines and
GeometryCollection are rejected at the schema boundary (422).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.schemas.place import PlaceCreate, _has_z, geometry_to_geojson, iter_positions

pytestmark = pytest.mark.unit


def _wkt_feature(wkt: str, **extra) -> dict:
    return {"type": "Feature", "geometry": wkt, "properties": {}, **extra}


_VALID_POLYGON = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
    },
    "properties": {},
}


def test_iter_positions_walks_polygon():
    positions = list(iter_positions([[[0, 0], [1, 0], [1, 1], [0, 0]]]))
    assert positions == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]


def test_iter_positions_walks_multipolygon():
    mp = [[[[0, 0], [1, 0], [1, 1], [0, 0]]], [[[5, 5], [6, 5], [6, 6], [5, 5]]]]
    positions = list(iter_positions(mp))
    assert (5.0, 5.0) in positions and (1.0, 1.0) in positions
    assert len(positions) == 8


def test_iter_positions_walks_point():
    assert list(iter_positions([3.0, 4.0])) == [(3.0, 4.0)]


def test_iter_positions_walks_multipoint():
    assert list(iter_positions([[0, 0], [5, 5]])) == [(0.0, 0.0), (5.0, 5.0)]


def test_accepts_valid_polygon_and_extracts_external_id_from_feature_id():
    feature = PlaceCreate.model_validate({**_VALID_POLYGON, "id": "plot-1"})
    assert feature.geometry.type == "Polygon"
    assert feature.external_id == "plot-1"


def test_external_id_is_none_when_feature_has_no_id():
    feature = PlaceCreate.model_validate(_VALID_POLYGON)
    assert feature.external_id is None


def test_feature_without_properties_is_accepted():
    # Deliberate RFC 7946 input leniency: an omitted properties member parses as null.
    feature = PlaceCreate.model_validate(
        {"type": "Feature", "geometry": _VALID_POLYGON["geometry"]}
    )
    assert feature.properties is None


def test_accepts_multipolygon():
    feature = PlaceCreate.model_validate(
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]],
            },
            "properties": {},
        }
    )
    assert feature.geometry.type == "MultiPolygon"


def test_accepts_point_geometry():
    feature = PlaceCreate.model_validate(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {},
        }
    )
    assert feature.geometry.type == "Point"


def test_accepts_multipoint():
    feature = PlaceCreate.model_validate(
        {
            "type": "Feature",
            "geometry": {"type": "MultiPoint", "coordinates": [[0, 0], [5, 5]]},
            "properties": {},
        }
    )
    assert feature.geometry.type == "MultiPoint"


def test_rejects_linestring_geometry():
    # Lines stay rejected by the supported-geometry discriminator.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {},
            }
        )


def test_rejects_multilinestring():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]]]},
                "properties": {},
            }
        )


def test_rejects_empty_multipoint():
    # RFC 7946 sets no minimum for MultiPoint; an empty one must not reach the DB as
    # a constant per-type WKB. geojson-pydantic rejects coordinates: [] at the schema.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": []},
                "properties": {},
            }
        )


def test_rejects_point_out_of_bounds():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [200, 0]},
                "properties": {},
            }
        )


def test_rejects_multipoint_out_of_bounds():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": [[0, 0], [0, 200]]},
                "properties": {},
            }
        )


def test_rejects_unclosed_ring():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
                "properties": {},
            }
        )


def test_rejects_out_of_bounds_longitude():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[200, 0], [201, 0], [201, 1], [200, 1], [200, 0]]],
                },
                "properties": {},
            }
        )


def test_rejects_out_of_bounds_latitude_likely_swapped_lonlat():
    # lat=200 is a classic lon/lat swap; must be rejected (fail fast, before DB).
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 200], [1, 200], [1, 201], [0, 201], [0, 200]]],
                },
                "properties": {},
            }
        )


# --- WKT string geometry (vendor extension) ---------------------------------


def test_accepts_wkt_string_polygon():
    feature = PlaceCreate.model_validate(_wkt_feature("POLYGON((10 10,11 10,11 11,10 11,10 10))"))
    assert feature.geometry.type == "Polygon"


def test_accepts_wkt_string_multipolygon():
    feature = PlaceCreate.model_validate(_wkt_feature("MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))"))
    assert feature.geometry.type == "MultiPolygon"


def test_wkt_feature_id_becomes_external_id():
    feature = PlaceCreate.model_validate(
        _wkt_feature("POLYGON((10 10,11 10,11 11,10 11,10 10))", id="plot-wkt")
    )
    assert feature.external_id == "plot-wkt"


def test_accepts_wkt_point():
    feature = PlaceCreate.model_validate(_wkt_feature("POINT(1 2)"))
    assert feature.geometry.type == "Point"


def test_rejects_wkt_geometrycollection_via_supported_discriminator():
    # Parses fine at the codec, rejected by the supported-geometry discriminator.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("GEOMETRYCOLLECTION(POINT(1 2))"))


def test_rejects_invalid_wkt_string():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("POLYGON((10 10,11"))


def test_rejects_ewkt_string():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            _wkt_feature("SRID=4326;POLYGON((10 10,11 10,11 11,10 11,10 10))")
        )


def test_rejects_out_of_bounds_wkt_via_lonlat_after_validator():
    # The mode="after" bounds check still runs on a WKT-sourced geometry.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("POLYGON((0 200,1 200,1 201,0 201,0 200))"))


def test_wkt_and_geojson_produce_identical_hash_text():
    # THE sacred parity: WKT and the equivalent GeoJSON converge on byte-identical
    # geometry text -> identical geoid_geom_hash_default -> same geoid / 201.
    from_wkt = PlaceCreate.model_validate(_wkt_feature("POLYGON((10 10,11 10,11 11,10 11,10 10))"))
    from_geojson = PlaceCreate.model_validate(_VALID_POLYGON)
    assert geometry_to_geojson(from_wkt) == geometry_to_geojson(from_geojson)


# --- 2D-only: any Z ordinate is rejected at the boundary (422) ---------------


def test_rejects_3d_point():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0, 5]},
                "properties": {},
            }
        )


def test_rejects_3d_polygon():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 1, 5], [0, 0, 5]]],
                },
                "properties": {},
            }
        )


def test_rejects_3d_multipoint():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": [[0, 0, 5], [5, 5, 5]]},
                "properties": {},
            }
        )


def test_rejects_mixed_2d_3d_vertices():
    # A single Z-bearing ring vertex among 2D ones must still be rejected — proves
    # the any-leaf detection, not just an all-or-nothing first-vertex check.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1, 5], [0, 1], [0, 0]]],
                },
                "properties": {},
            }
        )


def test_rejects_3d_wkt_string():
    # WKT 3D decodes to a Z-bearing coordinate dict via the mode="before" validator,
    # then hits the same mode="after" 2D-only check — proves WKT coverage.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("POINT Z (1 2 5)"))


def test_rejects_wkt_measured_point_m():
    # A measured (M) coordinate must be rejected, not silently dropped: dropping M
    # would mint POINT(1 2) — the silent-merge ADR-006 forbids. Today shapely
    # surfaces M as a 3rd ordinate that the 2D-only check trips; this test pins the
    # REJECTION outcome so a future shapely that *drops* M fails loudly here.
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("POINT M (1 2 3)"))


def test_rejects_wkt_point_zm():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(_wkt_feature("POINT ZM (1 2 3 4)"))


@pytest.mark.parametrize(
    ("coordinates", "expected"),
    [
        ([0, 0, 5], True),  # point
        ([[[0, 0, 5], [1, 0, 5], [0, 0, 5]]], True),  # polygon ring
        ([[[[0, 0, 5]]]], True),  # multipolygon
        ([[0, 0], [1, 1, 5]], True),  # mixed 2D/3D leaves
        ([0, 0], False),  # 2D point
        ([[[0, 0], [1, 0], [0, 0]]], False),  # 2D polygon ring
        ([], False),  # empty
    ],
)
def test_has_z_detects_any_3d_leaf(coordinates, expected):
    assert _has_z(coordinates) is expected


# --- Identity-lattice degeneracy: rejected at the schema boundary (ADR-007) ---


def _polygon_feature(coords) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {},
    }


def test_rejects_geojson_polygon_that_degenerates_on_the_lattice():
    # A valid sub-cell sliver collapses below 3 distinct 1e-7-lattice vertices:
    # it has no v2 identity, so the schema rejects it with a clear 422 message.
    with pytest.raises(ValidationError, match="identity precision"):
        PlaceCreate.model_validate(
            _polygon_feature([[[0, 0], [1, 0], [1, 1e-8], [0, 1e-8], [0, 0]]])
        )


def test_rejects_geojson_polygon_with_zero_lattice_area():
    with pytest.raises(ValidationError, match="identity precision"):
        PlaceCreate.model_validate(_polygon_feature([[[0, 0], [1, 0], [2, 1e-8], [0, 0]]]))


def test_rejects_wkt_polygon_that_degenerates_on_the_lattice():
    # Uniform for the WKT vendor extension: the same sliver as WKT text.
    with pytest.raises(ValidationError, match="identity precision"):
        PlaceCreate.model_validate(_wkt_feature("POLYGON((0 0,1 0,1 0.00000001,0 0.00000001,0 0))"))


def test_accepts_geometry_comfortably_above_the_lattice():
    # The pre-check must not reject ordinary geometry (the whole valid corpus
    # is separately pinned by the golden vectors).
    feature = PlaceCreate.model_validate(_VALID_POLYGON)
    assert feature.geometry.type == "Polygon"

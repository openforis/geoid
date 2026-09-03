"""Unit tests for the geometry-format codec layer (domain/geometry_format).

Covers the canonical-dict pivot (decode/encode), WKT edge cases (EWKT, 3D,
malformed, wrong-type input), round-tripping, and the content-negotiation
precedence matrix (``?f=`` overrides ``Accept``; unknown ``?f=`` raises).
"""

from __future__ import annotations

import pytest

from geoid.domain.geometry_format import (
    GeometryFormat,
    decode_geometry,
    encode_geometry,
    negotiate_format,
)

pytestmark = pytest.mark.unit

_WKT_POLYGON = "POLYGON((10 10,11 10,11 11,10 11,10 10))"
_WKT_MULTIPOLYGON = "MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))"
_GEOJSON_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
}


# --- decode ------------------------------------------------------------------


def test_decode_geojson_is_identity():
    assert decode_geometry(_GEOJSON_POLYGON) == _GEOJSON_POLYGON


def test_decode_wkt_polygon_to_geojson_dict():
    result = decode_geometry(_WKT_POLYGON)
    assert result["type"] == "Polygon"
    # shapely yields nested tuples; JSON-equivalent to GeoJSON arrays.
    assert result["coordinates"][0][0] == (10.0, 10.0)


def test_decode_wkt_multipolygon_to_geojson_dict():
    result = decode_geometry(_WKT_MULTIPOLYGON)
    assert result["type"] == "MultiPolygon"


def test_decode_wkt_3d_keeps_z_ordinate():
    # WKT/GeoJSON symmetry: geojson-pydantic accepts Position3D, so Z is preserved.
    result = decode_geometry("POLYGON Z ((0 0 1,1 0 1,1 1 1,0 1 1,0 0 1))")
    assert result["type"] == "Polygon"
    assert result["coordinates"][0][0] == (0.0, 0.0, 1.0)


def test_decode_rejects_ewkt():
    with pytest.raises(ValueError, match="invalid WKT geometry"):
        decode_geometry("SRID=4326;POLYGON((10 10,11 10,11 11,10 11,10 10))")


def test_decode_rejects_malformed_wkt():
    with pytest.raises(ValueError, match="invalid WKT geometry"):
        decode_geometry("POLYGON((10 10,11")


def test_decode_rejects_garbage_string():
    with pytest.raises(ValueError, match="invalid WKT geometry"):
        decode_geometry("not wkt at all")


def test_decode_geometrycollection_succeeds_at_codec_level():
    # The codec parses any valid WKT; unsupported types (lines, GeometryCollection)
    # are rejected one layer up by the schema's SupportedGeometry discriminator,
    # not here — keeping the codec format-only and the rejection uniform with GeoJSON.
    result = decode_geometry("GEOMETRYCOLLECTION(POINT(1 2))")
    assert result["type"] == "GeometryCollection"


@pytest.mark.parametrize("bad", [None, 123, 4.5, ["POLYGON"]])
def test_decode_rejects_non_str_non_dict(bad):
    with pytest.raises(ValueError, match="GeoJSON object or a WKT string"):
        decode_geometry(bad)


# --- encode + round-trip -----------------------------------------------------


def test_encode_geojson_dict_to_wkt():
    wkt = encode_geometry(_GEOJSON_POLYGON, GeometryFormat.WKT)
    assert wkt == "POLYGON ((10 10, 11 10, 11 11, 10 11, 10 10))"


def test_encode_geojson_format_is_identity():
    assert encode_geometry(_GEOJSON_POLYGON, GeometryFormat.GEOJSON) == _GEOJSON_POLYGON


def test_wkt_round_trip_is_stable():
    # decode(WKT) -> dict -> encode(dict) -> WKT -> decode again == same dict.
    once = decode_geometry(_WKT_POLYGON)
    again = decode_geometry(encode_geometry(once, GeometryFormat.WKT))
    assert once == again


# --- negotiate_format precedence matrix --------------------------------------


@pytest.mark.parametrize(
    ("f_param", "accept", "expected"),
    [
        (None, None, GeometryFormat.GEOJSON),
        (None, "*/*", GeometryFormat.GEOJSON),
        (None, "application/geo+json", GeometryFormat.GEOJSON),
        (None, "text/plain", GeometryFormat.WKT),
        (None, "text/plain; charset=utf-8", GeometryFormat.WKT),
        ("wkt", None, GeometryFormat.WKT),
        ("geojson", None, GeometryFormat.GEOJSON),
        ("WKT", None, GeometryFormat.WKT),  # case-insensitive
        # ?f= overrides Accept (both directions).
        ("geojson", "text/plain", GeometryFormat.GEOJSON),
        ("wkt", "application/geo+json", GeometryFormat.WKT),
    ],
)
def test_negotiate_format_precedence(f_param, accept, expected):
    assert negotiate_format(f_param, accept) is expected


@pytest.mark.parametrize("bad", ["xml", "csv", "", "shapefile"])
def test_negotiate_format_unknown_f_raises(bad):
    with pytest.raises(ValueError, match="unknown format"):
        negotiate_format(bad, None)


def test_negotiate_format_unknown_accept_defaults_to_geojson():
    # An Accept header naming no supported media type falls back to the default
    # encoding rather than erroring — only an explicit unknown ?f= is a 400.
    assert negotiate_format(None, "application/weird") is GeometryFormat.GEOJSON

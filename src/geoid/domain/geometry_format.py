"""Geometry serialization formats — the single home for all format knowledge.

GeoID moves geometry around as one canonical representation: the **GeoJSON
geometry dict** (``{"type": ..., "coordinates": ...}``). The database is the only
GeoJSON gateway (``ST_GeomFromGeoJSON`` in, ``ST_AsGeoJSON`` out) and the dedup
hash recipe is computed entirely from that — so every supported wire format is
defined purely as a *codec* that converts to/from that one dict, and nothing
outside this module needs to know a second format exists.

Adding a third format = add one ``GeometryCodec`` + one registry entry. Changing a
format's internals = edit one codec. The outer layers only ever call
``decode_geometry`` / ``encode_geometry`` / ``negotiate_format``.

WKT is a documented *vendor extension* on the OGC read surface (GeoJSON stays the
default and the only encoding in ``/conformance``). Plain WKT only — assumed
EPSG:4326, never reprojected, matching the no-transform contract; EWKT is rejected.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from shapely import from_wkt
from shapely.errors import GEOSException
from shapely.geometry import mapping, shape


class GeometryFormat(StrEnum):
    GEOJSON = "geojson"
    WKT = "wkt"


GEOJSON_MEDIA_TYPE = "application/geo+json"
WKT_MEDIA_TYPE = "text/plain"


class GeometryCodec(Protocol):
    """A bidirectional converter between a wire format and the canonical dict."""

    def decode(self, raw: dict | str) -> dict:
        """Parse the wire form into a canonical GeoJSON geometry dict."""
        ...

    def encode(self, geojson_geometry: dict) -> str | dict:
        """Render a canonical GeoJSON geometry dict into the wire form."""
        ...


class GeoJsonCodec:
    """Identity codec — GeoJSON *is* the canonical representation."""

    def decode(self, raw: dict | str) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("GeoJSON geometry must be an object")
        return raw

    def encode(self, geojson_geometry: dict) -> dict:
        return geojson_geometry


class WktCodec:
    """Well-Known Text codec, pivoting on shapely.

    ``decode`` keeps any Z ordinate (3D WKT) for format fidelity — the codec is a
    faithful WKT↔GeoJSON converter and does not mutate coordinates. Z is then
    **rejected one layer up by the schema's 2D-only validator** (``_has_z`` in
    ``schemas/place``), uniform for WKT and GeoJSON, so no Z reaches the DB.
    Point/MultiPoint/Polygon/MultiPolygon decode to a dict the schema accepts; an
    unsupported but parseable input (LineString, MultiLineString,
    GeometryCollection) decodes to a dict and is then rejected by the schema's
    supported-geometry discriminator, uniform with a GeoJSON line.
    """

    def decode(self, raw: dict | str) -> dict:
        if not isinstance(raw, str):
            raise ValueError("WKT geometry must be a string")
        try:
            return mapping(from_wkt(raw))
        except GEOSException as exc:
            # Malformed / EWKT (SRID=...) / non-WKT garbage all fail to parse here.
            raise ValueError(f"invalid WKT geometry: {exc}") from exc

    def encode(self, geojson_geometry: dict) -> str:
        try:
            return shape(geojson_geometry).wkt
        except (GEOSException, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"cannot encode geometry as WKT: {exc}") from exc


_REGISTRY: dict[GeometryFormat, GeometryCodec] = {
    GeometryFormat.GEOJSON: GeoJsonCodec(),
    GeometryFormat.WKT: WktCodec(),
}


def decode_geometry(raw: dict | str) -> dict:
    """Decode an incoming geometry to the canonical GeoJSON geometry dict.

    A GeoJSON object routes to the (identity) GeoJSON codec; a string routes to the
    WKT codec; anything else is a contract error. A malformed value raises
    ``ValueError`` which the schema layer surfaces as 422.
    """
    if isinstance(raw, dict):
        return _REGISTRY[GeometryFormat.GEOJSON].decode(raw)
    if isinstance(raw, str):
        return _REGISTRY[GeometryFormat.WKT].decode(raw)
    raise ValueError(f"geometry must be a GeoJSON object or a WKT string, got {type(raw).__name__}")


def encode_geometry(geojson_geometry: dict, fmt: GeometryFormat) -> str | dict:
    """Encode a canonical GeoJSON geometry dict into the requested format."""
    return _REGISTRY[fmt].encode(geojson_geometry)


def negotiate_format(f_param: str | None, accept_header: str | None) -> GeometryFormat:
    """Resolve the output format OGC-style: ``?f=`` wins, else ``Accept``, else GeoJSON.

    An explicit ``?f=`` overrides the ``Accept`` header (OGC API – Common §8.7);
    an unknown ``?f=`` value raises ``ValueError`` (the router maps it to 400,
    matching the project's closed-contract handling of bad query params). With no
    ``f``, a ``text/plain`` substring in ``Accept`` selects WKT; otherwise GeoJSON,
    the default encoding.
    """
    if f_param is not None:
        try:
            return GeometryFormat(f_param.lower())
        except ValueError as exc:
            supported = ", ".join(fmt.value for fmt in GeometryFormat)
            raise ValueError(f"unknown format '{f_param}'; supported: {supported}") from exc
    if accept_header and WKT_MEDIA_TYPE in accept_header.lower():
        return GeometryFormat.WKT
    return GeometryFormat.GEOJSON

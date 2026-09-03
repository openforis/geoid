"""Place schemas — the create request (a GeoJSON Feature) and the mint response."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from geojson_pydantic import Feature
from geojson_pydantic.geometries import MultiPoint, MultiPolygon, Point, Polygon
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoid.domain.geometry_format import decode_geometry
from geoid.domain.geometry_identity import DegenerateGeometryError, canonical_bytes
from geoid.domain.identifiers import derive_identifiers

# Accepted geometry types. Lines and GeometryCollection are NOT accepted and are
# rejected at the schema boundary (422).
SupportedGeometry = Point | MultiPoint | Polygon | MultiPolygon

_LON_MIN, _LON_MAX = -180.0, 180.0
_LAT_MIN, _LAT_MAX = -90.0, 90.0


def iter_positions(coordinates: Any) -> Iterator[tuple[float, float]]:
    """Yield every (lon, lat) position in an arbitrarily nested coordinate array.

    Works across Point ([lon,lat]), MultiPoint, Polygon (list[ring]) and
    MultiPolygon (list[polygon]) nesting; a leaf position is a sequence whose
    first element is a number.
    """
    if isinstance(coordinates, (list, tuple)) and coordinates:
        head = coordinates[0]
        if isinstance(head, (int, float)):
            yield (float(coordinates[0]), float(coordinates[1]))
            return
        for item in coordinates:
            yield from iter_positions(item)


def _has_z(coordinates: Any) -> bool:
    """True if any leaf position carries a 3rd (Z) ordinate. GeoID is 2D-only."""
    if isinstance(coordinates, (list, tuple)) and coordinates:
        head = coordinates[0]
        if isinstance(head, (int, float)):
            return len(coordinates) > 2
        return any(_has_z(item) for item in coordinates)
    return False


# Swagger "Try it out" bodies. Coordinates are obviously-dummy sequential-digit runs
# at the 7-decimal identity precision (1e-7° lattice) — executable, never a real place.
# The single and bulk polygons are DISTINCT so first-click single-then-bulk mint two
# geoids rather than converging on one (a repeat of either returns the same geoid).
_EXAMPLE_FEATURE = {
    "type": "Feature",
    "id": "my-plot-001",
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [12.3456789, 45.6789012],
                [12.456789, 45.6789012],
                [12.456789, 45.7890123],
                [12.3456789, 45.7890123],
                [12.3456789, 45.6789012],
            ]
        ],
    },
}


class PlaceCreate(Feature[SupportedGeometry, dict[str, Any] | None]):
    """Incoming GeoJSON Feature for a place.

    The optional GeoJSON ``id`` member becomes the place's ``external_id``
    (collection-scoped unique). Geometry must be a Point/MultiPoint/Polygon/
    MultiPolygon in EPSG:4326 (lines and GeometryCollection are rejected),
    supplied either as a GeoJSON geometry object **or** as a WKT string
    (a vendor extension; both single create and per-feature bulk). ``properties``
    is optional (a deliberate RFC 7946 input leniency); when provided it is
    accepted but never persisted — provenance records only
    schema/created_by/originating_instance.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_FEATURE})

    properties: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Supported per RFC 7946 but never persisted — always omitted if provided "
            "(provenance records only schema/created_by/originating_instance)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _decode_string_geometry(cls, data: Any) -> Any:
        """Accept a WKT-string ``geometry`` member by decoding it to GeoJSON first.

        Runs BEFORE geojson-pydantic parses, so a WKT polygon converges on the exact
        same geometry dict a GeoJSON polygon would — identical geometry text →
        identical ``geoid_geom_hash_default`` → same geoid / 201. A GeoJSON object
        ``geometry`` is left untouched (the default path is byte-for-byte unchanged);
        a malformed WKT string raises ``ValueError`` → ``ValidationError`` → 422.
        """
        if isinstance(data, dict) and isinstance(data.get("geometry"), str):
            return {**data, "geometry": decode_geometry(data["geometry"])}
        return data

    @model_validator(mode="after")
    def _validate_lonlat_bounds(self) -> PlaceCreate:
        if self.geometry is None:
            raise ValueError("geometry is required")
        positions = list(iter_positions(self.geometry.coordinates))
        if not positions:
            # An empty geometry (e.g. MultiPoint []) passes geojson-pydantic but would
            # hash to a constant per-type WKB and falsely dedup unrelated rows onto one
            # geoid. Reject here with a clear message; ck_place_geom_not_empty backstops.
            raise ValueError("geometry is empty: it carries no coordinates")
        if _has_z(self.geometry.coordinates):
            raise ValueError(
                "3D (Z) coordinates are not supported: GeoID is 2D-only "
                "(strip the altitude/elevation ordinate and resubmit)"
            )
        for lon, lat in positions:
            if not (_LON_MIN <= lon <= _LON_MAX) or not (_LAT_MIN <= lat <= _LAT_MAX):
                raise ValueError(
                    f"coordinate out of bounds: lon={lon}, lat={lat} "
                    f"(expected lon∈[{_LON_MIN},{_LON_MAX}], lat∈[{_LAT_MIN},{_LAT_MAX}]); "
                    "GeoJSON is lon,lat — check for a swapped pair"
                )
        # Identity-lattice degeneracy (recipe v2, ADR-007): a geometry whose ring
        # collapses below 3 distinct 1e-7-lattice vertices (or to zero lattice
        # area) has no v2 identity — reject with a clear 422 here (uniform for
        # GeoJSON + WKT, single + bulk) rather than let the DB backstop (GD001)
        # answer. Same rationale as the Z rejection above (ADR-006): an identity
        # service must not silently merge or vanish distinct submissions.
        try:
            canonical_bytes({"type": self.geometry.type, "coordinates": self.geometry.coordinates})
        except DegenerateGeometryError as exc:
            raise ValueError(str(exc)) from exc
        except ValueError:
            # Non-degeneracy complaints (unsupported type, structure) are already
            # owned by the discriminator/earlier checks — never duplicate them here.
            pass
        return self

    @property
    def external_id(self) -> str | None:
        return None if self.id is None else str(self.id)


def geometry_to_geojson(feature: PlaceCreate) -> str:
    """Serialise a feature's geometry to a GeoJSON geometry string for PostGIS.

    The SINGLE source for both the single-row and the bulk write paths (both reach
    ``place_repo.insert_place`` through ``registry_service``), so they stage
    byte-identical geometry text into ``ST_GeomFromGeoJSON`` — a divergence here
    would break the golden-vector hash parity the two paths share.
    """
    geom = feature.geometry
    return json.dumps({"type": geom.type, "coordinates": geom.coordinates})


class MintResponse(BaseModel):
    """Response to a successful POST (201): the resolvable forms of the geoid.

    The mint is idempotent: a geometry already registered answers with the same
    status and body shape as a first mint, carrying its existing geoid. The body
    reports the geoid, never where the row lives.
    """

    geoid: str = Field(
        description="The geoid — a content-addressed UUIDv8 derived from the geometry; "
        "the canonical, immutable identifier."
    )
    uri: str = Field(description="Durable resolver URI, e.g. https://data.fao.org/geoid/<uuid>.")
    external_id: str | None = Field(default=None)


class PlaceRecord(BaseModel):
    """A geometry-free place record — one row of a listing.

    Backs both ``GET /me/geoids`` (the caller's own mints) and the sysadmin
    ``GET /manage/collections/{id}/items`` inventory. Deliberately carries NO
    geometry: listings answer "which geoids", never "which shapes" — resolve a
    geoid for the feature itself.
    """

    geoid: str
    uri: str
    collection: str
    external_id: str | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any], *, base_url: str) -> PlaceRecord:
        ids = derive_identifiers(row["geoid"], base_url=base_url)
        return cls(
            geoid=ids["geoid"],
            uri=ids["uri"],
            collection=row["collection_slug"],
            external_id=row.get("external_id"),
            created_at=row["created_at"],
        )


# --- Bulk write (synchronous multi-geometry POST) ---------------------------

# Per-feature reject discriminator. Maps 1:1 to the single-row write path's status
# for the same failure: the 4xx classes below read exactly like the response a
# single POST would return, and internal_error mirrors the single-row 500 (an
# unrecognised integrity violation answers 500 there, one rejected row here).
BulkRejectReason = Literal[
    "schema_invalid",  # failed the PlaceCreate pydantic schema (single-row 422)
    "invalid_geometry",  # unsupported type (line/GC) / not ST_IsValid / unparseable GeoJSON (422)
    "external_id_conflict",  # duplicate (collection, external_id) — 409
    "geoid_conflict",  # geoid PK collision — 409 (backstop)
    "internal_error",  # unrecognised integrity constraint — logged, surfaced honestly
]


_EXAMPLE_FEATURE_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "my-point-001",
            "geometry": {"type": "Point", "coordinates": [12.3456789, 56.7890123]},
        },
        {
            "type": "Feature",
            "id": "my-plot-002",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [23.456789, 6.7890123],
                        [23.5678901, 6.7890123],
                        [23.5678901, 6.8901234],
                        [23.456789, 6.8901234],
                        [23.456789, 6.7890123],
                    ]
                ],
            },
        },
    ],
}


class BulkFeatureCollection(BaseModel):
    """A GeoJSON FeatureCollection (RFC 7946 §3.3) submitted to the bulk route.

    Each feature is validated individually, so one malformed feature is rejected on
    its own rather than failing the whole request. An empty ``features`` list or a
    wrong ``type`` is rejected up front with 422.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_FEATURE_COLLECTION})

    type: Literal["FeatureCollection"]
    features: list[dict[str, Any]] = Field(min_length=1)


class BulkAccepted(BaseModel):
    """A feature that minted a new geoid or matched an existing one (mirrors
    :class:`MintResponse`)."""

    index: int = Field(description="Zero-based position in the submitted features array.")
    geoid: str
    uri: str
    external_id: str | None = None


class BulkRejected(BaseModel):
    """A feature that did not mint, with the reason it was skipped.

    Partial success is the contract: one bad feature never aborts the batch. The
    fields mirror the single-row error envelope.
    """

    index: int = Field(description="Zero-based position in the submitted features array.")
    reason: BulkRejectReason
    detail: str | None = Field(default=None, description="Human-readable explanation.")
    external_id: str | None = None


class BulkSummary(BaseModel):
    received: int = Field(description="Features in the submitted FeatureCollection.")
    accepted: int = Field(
        description="Features that minted a new geoid or matched an existing geometry."
    )
    rejected: int = Field(description="Features skipped (see ``rejected`` for the reasons).")


class BulkReport(BaseModel):
    """Outcome of a synchronous bulk POST — always 200, partial success.

    Valid geometries are inserted; bad ones are reported with the same reason the
    single-item endpoint returns. ``accepted`` + ``rejected`` are disjoint and
    together account for every submitted feature (``summary.received``).
    ``accepted`` may carry the SAME geoid more than once — identical geometries in
    one batch all resolve to the one geoid that geometry mints.
    """

    summary: BulkSummary
    accepted: list[BulkAccepted] = Field(default_factory=list)
    rejected: list[BulkRejected] = Field(default_factory=list)

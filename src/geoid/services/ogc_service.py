"""OGC API Features response assembly (pure shaping; no DB access).

Builds landing page, conformance, collection descriptions, and the single GeoJSON
feature envelope (resolver / external-id lookup) with HATEOAS links. The item
listing / filtering / queryables surface has been removed, so the paging and CQL2
shaping helpers are gone with it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, TypedDict

from geoid.config import Settings
from geoid.domain.geometry_format import WKT_MEDIA_TYPE, GeometryFormat, encode_geometry
from geoid.domain.identifiers import uri_for
from geoid.schemas.ogc import (
    CollectionDesc,
    ConformanceDeclaration,
    Extent,
    FeatureModel,
    LandingPage,
    Link,
    SpatialExtent,
)

# OGC API - Features Part 1: Core + OAS30 + GeoJSON. The item read surface
# (Features listing / CQL2 filtering / queryables) has been removed, so Part 3
# (filter/queryables) and CQL2 are no longer advertised. Core/OAS30/GeoJSON still
# hold honestly: the landing page, /conformance, the collection-describe surface,
# and the GeoJSON resolver output.
CONFORMANCE_CLASSES = [
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
]

_GEOJSON = "application/geo+json"
_JSON = "application/json"


class ItemRow(TypedDict, total=False):
    """A place read row (single-item / resolver / external-id lookup path)."""

    geoid: uuid.UUID
    geometry: str | None
    external_id: str | None
    provenance: dict[str, Any]
    created_at: datetime
    originating_instance: str | None
    collection_slug: str
    collection_id: uuid.UUID


def landing_page(settings: Settings) -> LandingPage:
    base = settings.base_url_clean
    return LandingPage(
        title="GeoID — OGC API Features",
        description=(
            "A federated registry of immutable, resolvable geospatial place identifiers "
            "(geoids), served as OGC API Features."
        ),
        links=[
            Link(href=f"{base}/", rel="self", type=_JSON, title="This landing page"),
            Link(
                href=f"{base}/conformance",
                rel="conformance",
                type=_JSON,
                title="Conformance classes",
            ),
            # DELIBERATE: this rel="data" target is admin-gated (a stakeholder rule —
            # the public must not enumerate collections/items), so an anonymous OGC
            # client following it hits 401. Advertising the link keeps the landing
            # page honest about where the data surface lives for authorized callers.
            Link(href=f"{base}/collections", rel="data", type=_JSON, title="Collections"),
            Link(
                href=f"{base}/docs",
                rel="service-doc",
                type="text/html",
                title="API documentation (Swagger)",
            ),
            Link(
                href=f"{base}/openapi.json",
                rel="service-desc",
                type=_JSON,
                title="OpenAPI definition",
            ),
        ],
    )


def conformance() -> ConformanceDeclaration:
    return ConformanceDeclaration(conformsTo=list(CONFORMANCE_CLASSES))


def collection_desc(
    settings: Settings,
    *,
    slug: str,
    title: str | None,
    description: str | None = None,
    extent_bbox: tuple[float, float, float, float] | None = None,
) -> CollectionDesc:
    base = settings.base_url_clean
    extent = Extent()  # world default
    if extent_bbox is not None:
        extent = Extent(spatial=SpatialExtent(bbox=[list(extent_bbox)]))
    return CollectionDesc(
        id=slug,
        title=title or slug,
        description=description,
        extent=extent,
        links=[
            Link(href=f"{base}/collections/{slug}", rel="self", type=_JSON),
            Link(href=f"{base}/collections", rel="parent", type=_JSON),
        ],
    )


def _resolver_links(settings: Settings, geoid: uuid.UUID) -> list[Link]:
    """The caller-independent links every feature body carries (masked included).

    The durable resolver is the only resolution path, so it IS the feature's self
    link (the collection-scoped /items/{geoid} route was removed); WKT is a
    vendor-extension encoding, advertised per OGC alternate links.
    """
    resolver_url = f"{settings.base_url_clean}/{geoid}"
    return [
        Link(href=resolver_url, rel="self", type=_GEOJSON, title="GeoJSON"),
        Link(href=f"{resolver_url}?f=wkt", rel="alternate", type=WKT_MEDIA_TYPE, title="WKT"),
    ]


def build_feature(settings: Settings, row: ItemRow, *, full: bool = True) -> FeatureModel:
    """Assemble an OGC feature from a place read row.

    ``full=False`` is the masked, non-member body (metadata visibility is
    membership-based — sysadmin / creator / any grant; client ruling 2026-07-09):
    the bare geometry with properties exactly ``{geoid, uri}`` and links exactly
    self + the WKT alternate. No provenance, external_id, created_at, or
    collection link. The full branch carries server metadata only —
    submitted properties are never persisted (geoid-prov/0.2), so nothing is
    echoed back.
    """
    geoid: uuid.UUID = row["geoid"]
    geometry = json.loads(row["geometry"]) if row.get("geometry") else None

    if not full:
        return FeatureModel(
            id=str(geoid),
            geometry=geometry,
            properties={"geoid": str(geoid), "uri": uri_for(geoid, settings.base_url_clean)},
            links=_resolver_links(settings, geoid),
        )

    provenance = dict(row.get("provenance") or {})

    created_at = row.get("created_at")
    created_iso = created_at.isoformat() if isinstance(created_at, datetime) else created_at

    properties: dict[str, Any] = {
        "geoid": str(geoid),
        "uri": uri_for(geoid, settings.base_url_clean),
        "external_id": row.get("external_id"),
        "created_at": created_iso,
        "originating_instance": row.get("originating_instance"),
        "_geoid_provenance": provenance,
    }

    links = [
        *_resolver_links(settings, geoid),
        Link(
            href=f"{settings.base_url_clean}/collections/{row['collection_slug']}",
            rel="collection",
            type=_JSON,
        ),
    ]

    return FeatureModel(id=str(geoid), geometry=geometry, properties=properties, links=links)


# --- WKT vendor-extension shaping (resolver / external-id WKT path) -----------


def feature_to_wkt(feature: FeatureModel) -> str:
    """One feature's geometry as a single WKT line (defensive on null geometry)."""
    if feature.geometry is None:
        return ""
    return encode_geometry(feature.geometry, GeometryFormat.WKT)


def feature_geojson_alternate_header(feature: FeatureModel) -> str:
    """Reciprocal ``Link`` for a single bare-WKT item: its GeoJSON ``self`` href."""
    self_link = next((link for link in feature.links if link.rel == "self"), None)
    href = self_link.href if self_link is not None else ""
    return f'<{href}>; rel="alternate"; type="{_GEOJSON}"'

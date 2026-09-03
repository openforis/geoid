"""OGC API Features response models — landing, conformance, collections, feature.

HATEOAS ``links`` everywhere; a feature's ``self`` link is the durable resolver.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Link(BaseModel):
    href: str
    rel: str
    type: str | None = None
    title: str | None = None


class LandingPage(BaseModel):
    title: str
    description: str
    links: list[Link]


class ConformanceDeclaration(BaseModel):
    conformsTo: list[str]


class SpatialExtent(BaseModel):
    bbox: list[list[float]] = Field(default_factory=lambda: [[-180.0, -90.0, 180.0, 90.0]])
    crs: str = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"


class Extent(BaseModel):
    spatial: SpatialExtent = Field(default_factory=SpatialExtent)


class CollectionDesc(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    itemType: str = "feature"
    crs: list[str] = Field(default_factory=lambda: ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"])
    extent: Extent = Field(default_factory=Extent)
    links: list[Link] = Field(default_factory=list)


class CollectionsResponse(BaseModel):
    links: list[Link] = Field(default_factory=list)
    collections: list[CollectionDesc] = Field(default_factory=list)


class FeatureModel(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: dict[str, Any] | None
    properties: dict[str, Any] = Field(default_factory=dict)
    links: list[Link] = Field(default_factory=list)

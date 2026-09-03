"""OGC API Features read router — landing, conformance, collections.

The item read surface (Features listing / CQL2 filtering / queryables / single
feature) has been removed: no endpoint may enumerate, filter, or search a
collection's geometries. Resolving a geoid you already hold is the durable
``GET /{geoid}`` resolver (``api/places.py``), the only resolution path.
Listing/describing collections is kept but gated behind the admin key, matching
``GET /manage/collections``. Landing and ``/conformance`` stay public.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.ogc import (
    CollectionDesc,
    CollectionsResponse,
    ConformanceDeclaration,
    LandingPage,
    Link,
)
from geoid.services import ogc_service
from geoid.services.exceptions import CollectionNotFoundError

router = APIRouter(tags=["ogc"])


@router.get("/", response_model=LandingPage, summary="OGC API Features landing page")
async def landing(settings: Settings = Depends(get_settings)) -> LandingPage:
    return ogc_service.landing_page(settings)


@router.get("/conformance", response_model=ConformanceDeclaration)
async def conformance() -> ConformanceDeclaration:
    return ogc_service.conformance()


@router.get(
    "/collections",
    response_model=CollectionsResponse,
    dependencies=[Depends(require_admin)],
)
async def list_collections(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CollectionsResponse:
    collections = await collection_repo.list_all(session)
    base = settings.base_url_clean
    # One batched ST_Extent query for all collections (avoids the N+1 full scans).
    extents = await place_repo.collection_extents(session, [c.id for c in collections])
    descs = [
        ogc_service.collection_desc(
            settings, slug=c.slug, title=c.title, extent_bbox=extents.get(c.id)
        )
        for c in collections
    ]
    return CollectionsResponse(
        links=[
            Link(href=f"{base}/collections", rel="self", type="application/json"),
            Link(href=f"{base}/", rel="parent", type="application/json"),
        ],
        collections=descs,
    )


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionDesc,
    dependencies=[Depends(require_admin)],
)
async def describe_collection(
    collection_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CollectionDesc:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    extent = await place_repo.collection_extent(session, collection.id)
    return ogc_service.collection_desc(
        settings, slug=collection.slug, title=collection.title, extent_bbox=extent
    )

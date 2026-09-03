"""Management router (admin-gated) — collection create + list, item inventory.

Collections are the flat entry point: the catalog tier is a single internal row
(bootstrap-created) and is not exposed here. Item listing is sysadmin-only and
lives under ``/manage`` (2026-07-03 requirement — a narrowed reversal of the
earlier remove-all-enumeration ruling): geometry-free records, while the public
``GET /collections/{id}/items`` stays 405 (POST owns that path) and the OGC
conformance surface is untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import require_admin
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.collection import CollectionCreate, CollectionOut
from geoid.schemas.place import PlaceRecord
from geoid.services import listing_service
from geoid.services.exceptions import CollectionNotFoundError

router = APIRouter(prefix="/manage", tags=["manage"], dependencies=[Depends(require_admin)])


@router.post("/collections", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionCreate,
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    return await listing_service.create_collection(session, body)


@router.get(
    "/collections",
    response_model=list[CollectionOut],
    summary="List collections",
)
async def list_collections(session: AsyncSession = Depends(get_session)) -> list[CollectionOut]:
    return await listing_service.list_collections(session)


@router.get(
    "/collections/{collection_id}/items",
    response_model=list[PlaceRecord],
    summary="List a collection's items (sysadmin-only inventory, newest first)",
    description=(
        "Geometry-free records — an inventory of which geoids a collection holds, "
        "not a feature listing. The public ``GET /collections/{id}/items`` remains "
        "405 (no public enumeration surface); resolve a geoid for its geometry."
    ),
)
async def list_collection_items(
    collection_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[PlaceRecord]:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    rows = await place_repo.list_by_collection(session, collection.id, limit=limit, offset=offset)
    return [PlaceRecord.from_row(row, base_url=settings.base_url_clean) for row in rows]

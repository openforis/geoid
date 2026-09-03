"""Listing service — backs the 1.2 management slice (admin-gated)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Collection, CollectionGrant
from geoid.repositories import catalog_repo, collection_repo
from geoid.schemas.collection import CollectionCreate, CollectionOut, GrantOut


def _collection_out(collection: Collection) -> CollectionOut:
    return CollectionOut(
        id=collection.slug,
        title=collection.title,
        public_write=collection.public_write,
        public_read=collection.public_read,
        metadata=collection.meta,
    )


def grant_out(grant: CollectionGrant) -> GrantOut:
    return GrantOut(
        email=grant.principal_email,
        role=grant.role,
        subject=grant.principal_subject,
        granted_by=grant.granted_by,
        created_at=grant.created_at,
    )


async def create_collection(session: AsyncSession, body: CollectionCreate) -> CollectionOut:
    """Create a collection under the single internal catalog (admin-gated write).

    The catalog tier is a create-once internal row, so this resolves it via
    ``get_or_create_default`` before inserting the collection — the management
    surface stays flat (collections only). A duplicate ``id`` raises the catalog-slug
    IntegrityError, mapped to 409 in ``api/errors.py``.
    """
    catalog = await catalog_repo.get_or_create_default(session)
    collection = await collection_repo.create(
        session,
        catalog_id=catalog.id,
        slug=body.id,
        title=body.title,
        public_write=body.public_write,
        public_read=body.public_read,
        metadata=body.metadata,
    )
    return _collection_out(collection)


async def list_collections(session: AsyncSession) -> list[CollectionOut]:
    collections = await collection_repo.list_all(session)
    return [_collection_out(c) for c in collections]

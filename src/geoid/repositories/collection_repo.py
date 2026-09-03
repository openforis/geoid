"""Collection data access.

OGC API Features addresses collections by a flat ``{collectionId}``; we treat the
collection ``slug`` as that id. Slug uniqueness is per-catalog (the DB
constraint), so a multi-catalog deployment could in principle have a slug
collision — :func:`get_by_slug` resolves the first match and the single default
catalog makes this unambiguous in the default single-catalog deployment. Pass
``catalog_id`` to scope it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.models import Collection


async def get_by_slug(
    session: AsyncSession, slug: str, *, catalog_id: uuid.UUID | None = None
) -> Collection | None:
    stmt = select(Collection).where(Collection.slug == slug)
    if catalog_id is not None:
        stmt = stmt.where(Collection.catalog_id == catalog_id)
    stmt = stmt.order_by(Collection.slug.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Collection]:
    stmt = select(Collection).order_by(Collection.slug.asc())
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    catalog_id: uuid.UUID,
    slug: str,
    title: str | None = None,
    public_write: bool = False,
    public_read: bool = True,
    metadata: dict[str, Any] | None = None,
) -> Collection:
    collection = Collection(
        id=uuid7(),
        catalog_id=catalog_id,
        slug=slug,
        title=title,
        public_write=public_write,
        public_read=public_read,
        meta=metadata or {},
    )
    session.add(collection)
    await session.flush()
    return collection

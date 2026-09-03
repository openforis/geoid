"""Catalog data access.

The catalog is a single, internal create-once row — the anchor that scopes
collections (and the seam for a future multi-catalog/federated deployment). It is
**not** part of the API surface — collections are the flat entry point. Bootstrap
guarantees the one default catalog via :func:`get_or_create_default`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.models import Catalog

DEFAULT_CATALOG_SLUG = "geoid"


async def get_by_slug(session: AsyncSession, slug: str) -> Catalog | None:
    stmt = select(Catalog).where(Catalog.slug == slug)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create(session: AsyncSession, *, slug: str, title: str | None = None) -> Catalog:
    catalog = Catalog(id=uuid7(), slug=slug, title=title, meta={})
    session.add(catalog)
    await session.flush()
    return catalog


async def get_or_create_default(session: AsyncSession) -> Catalog:
    """Return the single default catalog, creating it on first call (idempotent)."""
    catalog = await get_by_slug(session, DEFAULT_CATALOG_SLUG)
    if catalog is None:
        catalog = await create(session, slug=DEFAULT_CATALOG_SLUG, title="GeoID")
    return catalog

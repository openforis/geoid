"""Startup bootstrap — guarantee the reserved anonymous collection exists.

Anonymous contributions land in one reserved ``public`` collection
(``public_write=true``). The slug is config-driven (``GEOID_PUBLIC_COLLECTION``),
so seeding lives here (idempotent) rather than in a migration.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.models import Collection
from geoid.repositories import catalog_repo, collection_repo


async def ensure_public_collection(session: AsyncSession, settings: Settings) -> Collection:
    """Create the default catalog + reserved public collection if missing.

    Concurrency-tolerant: two cold-starting instances can both see the collection
    missing and race the create — the loser's IntegrityError rolls back and adopts
    the winner's row instead of crashing startup.
    """
    catalog = await catalog_repo.get_or_create_default(session)

    collection = await collection_repo.get_by_slug(
        session, settings.public_collection, catalog_id=catalog.id
    )
    if collection is None:
        try:
            collection = await collection_repo.create(
                session,
                catalog_id=catalog.id,
                slug=settings.public_collection,
                title="Public (anonymous contributions)",
                public_write=True,
            )
        except IntegrityError:
            await session.rollback()
            catalog = await catalog_repo.get_or_create_default(session)
            collection = await collection_repo.get_by_slug(
                session, settings.public_collection, catalog_id=catalog.id
            )
            if collection is None:  # not the cold-start race — surface the truth
                raise
    return collection

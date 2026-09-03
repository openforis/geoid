"""Alembic environment — async (asyncpg) so production needs no extra driver.

The URL is read from GeoID settings (``GEOID_DATABASE_URL``), not from alembic.ini,
so the same migration run works in every deployment.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# Import models so their tables register on Base.metadata (used for autogenerate).
import geoid.models  # noqa: F401  (side-effect import)
from geoid.config import DatabaseSettings
from geoid.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # DatabaseSettings, not Settings: migrations must not require app-level
    # config (e.g. the OIDC issuer/JWKS URL) to reach the database.
    return DatabaseSettings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = create_async_engine(_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Async database engine, session factory, and the ORM declarative base.

A single async engine per process. Sessions are request-scoped and yielded by
:func:`get_session` (a FastAPI dependency). Models inherit :class:`Base`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from geoid.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for all GeoID ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: Settings) -> AsyncEngine:
    # Server-side timeouts cap how long any one statement runs and how long a
    # connection may sit idle inside a transaction — this bounds the blast radius
    # of a stalled request; the longest-lived transaction today is the synchronous
    # bulk POST, which holds one pooled connection for its whole batch.
    server_settings: dict[str, str] = {"application_name": f"geoid:{settings.instance_id}"}
    if settings.db_statement_timeout_ms:
        server_settings["statement_timeout"] = str(settings.db_statement_timeout_ms)
    if settings.db_idle_in_tx_timeout_ms:
        server_settings["idle_in_transaction_session_timeout"] = str(
            settings.db_idle_in_tx_timeout_ms
        )
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        future=True,
        connect_args={"server_settings": server_settings},
    )


# No lock needed: check-then-assign is synchronous (no `await` between the `is None`
# test and the assignment), so on the single event loop one coroutine can't preempt
# another mid-init; lifespan also warms these before the app serves.
def get_engine() -> AsyncEngine:
    """Lazily build (once) and return the process-wide async engine."""
    global _engine
    if _engine is None:
        _engine = _build_engine(get_settings())
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Lazily build (once) and return the process-wide async session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session, commit on success, roll back on error."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine on shutdown (releases the connection pool)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None

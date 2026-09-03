"""Collection-grant data access (per-collection RBAC).

Grants are keyed by a NORMALISED email (``strip().lower()``) so case/whitespace
variants of the same address collapse onto one row. ``collection_grant`` is mutable:
``upsert_grant`` updates the role on conflict, ``delete_grant`` revokes, and
``backfill_subject`` records the Keycloak ``sub`` the first time a grantee with a
null subject is authorized.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.domain.identifiers import uuid7
from geoid.domain.roles import Role
from geoid.models import CollectionGrant

_USER = "user"


def _norm(email: str) -> str:
    return email.strip().lower()


async def get_grant(
    session: AsyncSession, collection_id: uuid.UUID, email: str
) -> CollectionGrant | None:
    """The caller's user grant on a collection (by normalised email), or None."""
    stmt = select(CollectionGrant).where(
        CollectionGrant.collection_id == collection_id,
        CollectionGrant.principal_type == _USER,
        CollectionGrant.principal_email == _norm(email),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_grants(session: AsyncSession, collection_id: uuid.UUID) -> list[CollectionGrant]:
    stmt = (
        select(CollectionGrant)
        .where(CollectionGrant.collection_id == collection_id)
        .order_by(CollectionGrant.created_at.asc(), CollectionGrant.principal_email.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_grant(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    email: str,
    role: str,
    granted_by: str | None = None,
) -> CollectionGrant:
    """Create or update a user grant (ON CONFLICT → update role/granted_by).

    ``principal_subject`` is never written here — it starts null and is recorded
    by :func:`backfill_subject` on the grantee's first authorized access.
    """
    stmt = (
        pg_insert(CollectionGrant)
        .values(
            id=uuid7(),
            collection_id=collection_id,
            principal_type=_USER,
            principal_email=_norm(email),
            role=role,
            granted_by=granted_by,
        )
        .on_conflict_do_update(
            constraint="uq_collection_grant_principal",
            set_={"role": role, "granted_by": granted_by},
        )
        .returning(CollectionGrant)
    )
    # populate_existing: without it the RETURNING row is served from the identity
    # map, so a role change upserted after get_grant() loaded the old row (the
    # last-owner check does) would return the STALE role in the response body.
    return (await session.execute(stmt, execution_options={"populate_existing": True})).scalar_one()


async def count_owners(session: AsyncSession, collection_id: uuid.UUID) -> int:
    """How many owner grants a collection has (the last-owner guard's input)."""
    stmt = (
        select(func.count())
        .select_from(CollectionGrant)
        .where(
            CollectionGrant.collection_id == collection_id,
            CollectionGrant.role == Role.OWNER.value,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def delete_grant(session: AsyncSession, collection_id: uuid.UUID, email: str) -> bool:
    """Revoke a user grant. Returns True if a row was deleted."""
    stmt = delete(CollectionGrant).where(
        CollectionGrant.collection_id == collection_id,
        CollectionGrant.principal_type == _USER,
        CollectionGrant.principal_email == _norm(email),
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def backfill_subject(
    session: AsyncSession, collection_id: uuid.UUID, email: str, subject: str
) -> None:
    """Record the Keycloak ``sub`` on a grant whose subject is still null.

    Guarded ``principal_subject IS NULL`` so a known subject is never overwritten
    (and concurrent backfills are idempotent).
    """
    stmt = (
        update(CollectionGrant)
        .where(
            CollectionGrant.collection_id == collection_id,
            CollectionGrant.principal_type == _USER,
            CollectionGrant.principal_email == _norm(email),
            CollectionGrant.principal_subject.is_(None),
        )
        .values(principal_subject=subject)
    )
    await session.execute(stmt)

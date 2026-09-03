"""Per-collection grant management (owner or sysadmin).

Collections are created via the admin ``/manage/collections`` alias; this router
manages the **grant ladder** on an existing collection — listing, granting/
re-granting a role by email, and revoking. Each route authorizes itself via
:mod:`geoid.services.authz_service` (the router carries no blanket dependency), and
the method+path of every route is distinct from the OGC GET read surface, so nothing
here shadows it. Mounted before the ``places`` ``/{geoid}`` catch-all so these literal
``/collections/...`` routes win.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.db import get_session
from geoid.deps import Principal, require_principal
from geoid.domain.roles import Role
from geoid.models import Collection
from geoid.repositories import collection_repo, grant_repo
from geoid.schemas.collection import GrantCreate, GrantOut
from geoid.services import authz_service, listing_service
from geoid.services.exceptions import (
    CollectionForbiddenError,
    CollectionNotFoundError,
    GrantNotFoundError,
    LastOwnerGuardError,
    OwnerGrantForbiddenError,
)

router = APIRouter(tags=["collections"])


async def _require_manageable(
    session: AsyncSession, collection_id: str, principal: Principal
) -> Collection:
    """Load a collection and assert the caller is its owner (or sysadmin).

    Anonymous → 401 first (matching ``require_admin``'s split — you can't be
    forbidden before you've authenticated); then 404 if the collection does not
    exist; then 403 (``CollectionForbiddenError``) if the caller is not
    owner/sysadmin — management ops surface forbidden, not the read-path's hide-existence.
    """
    if principal.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    grant = await authz_service.load_caller_grant(
        session, principal, collection.id, collection.slug
    )
    if not authz_service.can_manage(principal, collection, grant):
        raise CollectionForbiddenError(collection_id)
    return collection


@router.post(
    "/collections/{collection_id}/grants",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Grant or update a per-collection role by email (owner or sysadmin; "
    "the owner role itself is sysadmin-only)",
    description=(
        "Roles: `owner` (manage grants + write + read), `editor` (write + read), "
        "`viewer` (read: full feature bodies on caller-aware managed surfaces). "
        "Granting `owner` requires sysadmin (403 otherwise) — a collection owner "
        "may grant `editor`/`viewer` only. "
        "Demoting the last owner is blocked (409); grant another owner first."
    ),
)
async def create_grant(
    collection_id: str,
    body: GrantCreate,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> GrantOut:
    collection = await _require_manageable(session, collection_id, principal)
    # 401→404→403 precedence is _require_manageable's; only then the owner-role
    # gate, so a non-owner probing with role=owner still sees the manage 403.
    if body.role == Role.OWNER.value and not principal.is_admin:
        raise OwnerGrantForbiddenError(collection_id)
    if body.role != Role.OWNER.value:
        # Last-owner guard: an upsert that would demote the only owner is blocked
        # (sysadmin included — grant a second owner first).
        existing = await grant_repo.get_grant(session, collection.id, body.email)
        if (
            existing is not None
            and existing.role == Role.OWNER.value
            and await grant_repo.count_owners(session, collection.id) == 1
        ):
            raise LastOwnerGuardError(collection_id, existing.principal_email)
    grant = await grant_repo.upsert_grant(
        session,
        collection_id=collection.id,
        email=body.email,
        role=body.role,
        granted_by=principal.email or principal.subject,
    )
    return listing_service.grant_out(grant)


@router.get(
    "/collections/{collection_id}/grants",
    response_model=list[GrantOut],
    summary="List a collection's grants (owner or sysadmin)",
)
async def list_grants(
    collection_id: str,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> list[GrantOut]:
    collection = await _require_manageable(session, collection_id, principal)
    grants = await grant_repo.list_grants(session, collection.id)
    return [listing_service.grant_out(g) for g in grants]


@router.delete(
    "/collections/{collection_id}/grants/{email}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a per-collection grant by email (owner or sysadmin)",
)
async def delete_grant(
    collection_id: str,
    email: str,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    collection = await _require_manageable(session, collection_id, principal)
    existing = await grant_repo.get_grant(session, collection.id, email)
    if existing is None:
        raise GrantNotFoundError(collection_id, email)
    if (
        existing.role == Role.OWNER.value
        and await grant_repo.count_owners(session, collection.id) == 1
    ):
        # Last-owner guard: revoking the only owner would leave the collection
        # manageable by sysadmin alone. No bypass — grant another owner first.
        raise LastOwnerGuardError(collection_id, existing.principal_email)
    if not await grant_repo.delete_grant(session, collection.id, email):
        raise GrantNotFoundError(collection_id, email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

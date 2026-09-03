"""The registry service — the product: mint + dedup + provenance.

Anonymous and managed contributions share this ONE code path; the only branch is
the data-layer policy check (``public_write``). The geoid/dedup/external-id rules
are identical for both — anonymity is not a special case.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.deps import Principal
from geoid.domain.geometry_identity import DEGENERATE_MESSAGE
from geoid.domain.identifiers import derive_identifiers
from geoid.domain.provenance import build_provenance
from geoid.models import (
    PK_GEOID_REGISTRY,
    PK_PLACE,
    UQ_GEOID_REGISTRY_GEOM_HASH,
    UQ_PLACE_EXTERNAL_ID,
    Collection,
)
from geoid.repositories import place_repo
from geoid.repositories._pg_errors import (
    GEOJSON_PARSE_SQLSTATES,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEGENERATE_GEOMETRY,
    pg_fields,
    sqlstate_of,
)
from geoid.schemas.place import (
    BulkAccepted,
    BulkRejected,
    BulkReport,
    BulkSummary,
    MintResponse,
    PlaceCreate,
    geometry_to_geojson,
)
from geoid.services import authz_service
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    GeometryInvalidError,
    RegistryConsistencyError,
    WriteNotAuthorizedError,
)

logger = logging.getLogger(__name__)

# Geometry validity (ST_IsValid), supported-type (Point/MultiPoint/Polygon/
# MultiPolygon — lines and GeometryCollection rejected), and non-empty are enforced
# by CHECK constraints on the INSERT (SQLSTATE 23514). The supported-type gate +
# lon/lat bounds + RFC 7946 structure are ALSO enforced earlier by the PlaceCreate
# pydantic schema (422 before the DB).


def _build_write_inputs(
    feature: PlaceCreate, principal: Principal, settings: Settings
) -> tuple[str, str | None, dict[str, Any]]:
    """The ``(geojson, external_id, provenance)`` triple both write paths stage.

    One helper so the single and bulk paths can never drift on what they hash or
    record — the same reason both funnel into ``place_repo.insert_place``.
    """
    provenance = build_provenance(
        created_by=principal.subject,
        originating_instance=settings.instance_id,
    )
    return geometry_to_geojson(feature), feature.external_id, provenance


async def _authorize_write(
    session: AsyncSession, principal: Principal, collection: Collection
) -> None:
    """Gate a write on the per-collection authz ladder (sysadmin > editor/owner >
    public_write). Anonymous denial keeps its own 401-paired error; an authenticated
    non-grantee facing a non-writable collection gets the 403 write error.
    """
    grant = await authz_service.load_caller_grant(
        session, principal, collection.id, collection.slug
    )
    if authz_service.can_write(principal, collection, grant):
        return
    if principal.is_anonymous:
        raise AnonymousWriteForbiddenError(collection.slug)
    raise WriteNotAuthorizedError(collection.slug)


def _lost_registry_race(exc: IntegrityError) -> bool:
    """The multi-unique-index gap in the arbiter CTE's ``ON CONFLICT``.

    The arbiter names only the geom_hash UNIQUE, but an identical-geometry loser
    inserts the same derived geoid too and can trip the registry PK instead —
    an index the ``ON CONFLICT`` clause doesn't cover. The 23505 fires only after
    the winner commits, so ONE retry deterministically takes the arbiter's
    DO NOTHING path and recovers the normal dedup result.
    """
    constraint = pg_fields(exc)[0]
    if constraint not in (PK_GEOID_REGISTRY, UQ_GEOID_REGISTRY_GEOM_HASH):
        return False
    logger.warning("insert lost the registry unique race on %s; retrying once", constraint)
    return True


async def create_place(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    feature: PlaceCreate,
) -> MintResponse:
    """Mint a geoid for ``feature`` in ``collection``.

    The mint is IDEMPOTENT: an identical geometry already registered anywhere in
    the catalog returns the incumbent geoid with the same 201 and body shape as a
    first mint — no duplicate signal, no 409.

    Raises:
        AnonymousWriteForbiddenError: anon POST to a non-anonymous collection.
        WriteNotAuthorizedError: authenticated caller without an editor/owner grant
            POSTing to a non-writable collection.
        GeometryInvalidError: geometry unparseable / invalid (422 with reason).
    """
    await _authorize_write(session, principal, collection)

    geojson, external_id, provenance = _build_write_inputs(feature, principal, settings)

    insert_kwargs: dict[str, Any] = {
        "collection_id": collection.id,
        "geojson": geojson,
        "external_id": external_id,
        "provenance": provenance,
        "originating_instance": settings.instance_id,
    }
    # Happy path is one INSERT round-trip: the DB CHECK constraints reject invalid
    # geometry (23514) and we recover ST_IsValidReason for the 422 ONLY on that
    # error path — so a valid POST never pays a separate pre-validation query. The
    # geoid is derived from the geometry inside the insert (DB-side), not minted here.
    try:
        result = await place_repo.insert_place(session, **insert_kwargs)
    except IntegrityError as exc:
        if sqlstate_of(exc) == SQLSTATE_CHECK_VIOLATION:
            await session.rollback()
            reason = await place_repo.geometry_invalid_reason(session, geojson)
            raise GeometryInvalidError(reason) from exc
        if not _lost_registry_race(exc):
            raise  # 23505 (external_id / place-pk duplicate) → mapped to 409 by api/errors.py
        await session.rollback()
        result = await place_repo.insert_place(session, **insert_kwargs)
    except (OperationalError, InterfaceError):
        raise  # genuine infra failure — never mask as a 422
    except DBAPIError as exc:
        if sqlstate_of(exc) == SQLSTATE_DEGENERATE_GEOMETRY:
            # DB backstop (the schema pre-check rejects these first): a valid
            # geometry that degenerates on the identity lattice (GD001, recipe v2).
            await session.rollback()
            raise GeometryInvalidError(DEGENERATE_MESSAGE) from exc
        if sqlstate_of(exc) not in GEOJSON_PARSE_SQLSTATES:
            # Programming/schema drift (e.g. a missing SQL function, 42883) is NOT
            # the client's geometry — re-raise for a loud 500, never a silent 422.
            raise
        # Malformed GeoJSON makes ST_GeomFromGeoJSON raise and aborts the tx.
        logger.warning("geometry rejected: ST_GeomFromGeoJSON parse failure", exc_info=exc)
        await session.rollback()
        raise GeometryInvalidError("unparseable GeoJSON geometry") from exc

    if not result.created and result.collection_slug is None:
        # Genuine registry/recipe drift — the repo couldn't resolve the incumbent
        # place. A 201 whose geoid does not resolve would be worse than an error:
        # surface a structured 500 instead.
        logger.error("registry drift: dedup loser geoid=%s has no incumbent place", result.geoid)
        raise RegistryConsistencyError(result.geoid)

    ids = derive_identifiers(result.geoid, base_url=settings.base_url_clean)
    return MintResponse(
        geoid=ids["geoid"],
        uri=ids["uri"],
        external_id=external_id,
    )


async def create_places_bulk(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    features: list[dict[str, Any]],
) -> BulkReport:
    """Mint geoids for many features in ONE request, synchronously (partial success).

    Threads the SAME single-row building blocks as :func:`create_place` (hashing,
    dedup, provenance, identifiers) so the two write paths can never drift. Each
    feature runs inside its own SAVEPOINT: an aborting insert (external_id / CHECK /
    malformed GeoJSON) rolls back just that feature and the batch continues, while a
    geometry duplicate is swallowed by the arbiter CTE without aborting at all and is
    accepted with the incumbent geoid. The report always answers 200 — valid
    geometries are inserted, bad ones reported with the same reason the single-item
    endpoint returns.

    Raises:
        AnonymousWriteForbiddenError / WriteNotAuthorizedError: the caller may not
            write to this collection. Auth depends on principal + collection only (not
            the features), so it is one fail-fast check up front (403) rather than a
            per-feature reject.
    """
    await _authorize_write(session, principal, collection)

    accepted: list[BulkAccepted] = []
    rejected: list[BulkRejected] = []

    for index, raw in enumerate(features):
        try:
            feature = PlaceCreate.model_validate(raw)
        except ValidationError as exc:
            rejected.append(
                BulkRejected(index=index, reason="schema_invalid", detail=_validation_detail(exc))
            )
            continue

        outcome = await _mint_one(
            session,
            settings=settings,
            principal=principal,
            collection=collection,
            index=index,
            feature=feature,
        )
        (accepted if isinstance(outcome, BulkAccepted) else rejected).append(outcome)

    # get_session() owns the transaction boundary and commits on success (matching
    # create_place). Rejected rows left no trace — their per-feature SAVEPOINTs already
    # rolled back — so that commit persists only the accepted winners.
    return BulkReport(
        summary=BulkSummary(received=len(features), accepted=len(accepted), rejected=len(rejected)),
        accepted=accepted,
        rejected=rejected,
    )


async def _mint_one(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    index: int,
    feature: PlaceCreate,
) -> BulkAccepted | BulkRejected:
    """Insert one validated feature inside a SAVEPOINT; classify the outcome.

    The SAVEPOINT (``begin_nested``) is the correctness hinge: a geom_hash duplicate
    is swallowed by the arbiter CTE (``result.created=False``, no abort), but an
    external_id / CHECK / malformed-GeoJSON insert ABORTS the statement — without the
    SAVEPOINT that abort would poison the whole batch. We deliberately do NOT call
    ``session.rollback()`` here: letting ``begin_nested()``'s context manager roll the
    SAVEPOINT back on exception leaves the OUTER transaction healthy for the next
    feature, while a full rollback would discard every already-accepted row.
    """
    geojson, external_id, provenance = _build_write_inputs(feature, principal, settings)

    async def _attempt() -> place_repo.InsertResult:
        async with session.begin_nested():
            return await place_repo.insert_place(
                session,
                collection_id=collection.id,
                geojson=geojson,
                external_id=external_id,
                provenance=provenance,
                originating_instance=settings.instance_id,
            )

    try:
        result = await _attempt()
    except IntegrityError as exc:
        # The SAVEPOINT has already rolled back; the session is usable again, so a
        # CHECK violation can recover ST_IsValidReason exactly as the single row does.
        if not _lost_registry_race(exc):
            return await _classify_integrity(session, index, external_id, geojson, exc)
        try:
            result = await _attempt()
        except IntegrityError as retry_exc:
            return await _classify_integrity(session, index, external_id, geojson, retry_exc)
    except (OperationalError, InterfaceError):
        raise  # genuine infra failure — never mask as a rejected row
    except DBAPIError as exc:
        if sqlstate_of(exc) == SQLSTATE_DEGENERATE_GEOMETRY:
            # DB backstop, bulk twin: the SAVEPOINT already rolled back, so this
            # is one rejected row, mirroring the single-row 422.
            return BulkRejected(
                index=index,
                reason="invalid_geometry",
                detail=DEGENERATE_MESSAGE,
                external_id=external_id,
            )
        if sqlstate_of(exc) not in GEOJSON_PARSE_SQLSTATES:
            # Programming/schema drift — abort the batch loudly; a mislabelled
            # per-feature invalid_geometry would hide a server-side outage.
            raise
        # Malformed GeoJSON makes ST_GeomFromGeoJSON raise and aborts the statement.
        logger.warning(
            "bulk feature %d rejected: ST_GeomFromGeoJSON parse failure", index, exc_info=exc
        )
        return BulkRejected(
            index=index,
            reason="invalid_geometry",
            detail="unparseable GeoJSON geometry",
            external_id=external_id,
        )

    if not result.created and result.collection_slug is None:
        # Genuine registry/recipe drift — abort the batch with a structured 500
        # rather than accepting a geoid that does not resolve.
        logger.error(
            "registry drift: dedup loser geoid=%s has no incumbent place (bulk index %d)",
            result.geoid,
            index,
        )
        raise RegistryConsistencyError(result.geoid)

    ids = derive_identifiers(result.geoid, base_url=settings.base_url_clean)
    return BulkAccepted(
        index=index,
        geoid=ids["geoid"],
        uri=ids["uri"],
        external_id=external_id,
    )


async def _classify_integrity(
    session: AsyncSession,
    index: int,
    external_id: str | None,
    geojson: str,
    exc: IntegrityError,
) -> BulkRejected:
    """Map an aborting INSERT's IntegrityError to a per-feature reject (1:1 with 409/422)."""
    constraint, sqlstate = pg_fields(exc)
    if constraint == UQ_PLACE_EXTERNAL_ID:
        return BulkRejected(
            index=index,
            reason="external_id_conflict",
            detail="external_id already exists in this collection",
            external_id=external_id,
        )
    if constraint in (PK_PLACE, PK_GEOID_REGISTRY):
        return BulkRejected(index=index, reason="geoid_conflict", external_id=external_id)
    if sqlstate == SQLSTATE_CHECK_VIOLATION:
        reason = await place_repo.geometry_invalid_reason(session, geojson)
        return BulkRejected(
            index=index, reason="invalid_geometry", detail=reason, external_id=external_id
        )
    # Unrecognised integrity constraint — should not happen. Log it (with the stack
    # and Postgres DETAIL) and surface an honest internal_error (not a mislabelled
    # invalid_geometry); the batch survives as one rejected row rather than aborting.
    logger.error(
        "unclassified integrity violation: constraint=%r sqlstate=%r",
        constraint,
        sqlstate,
        exc_info=exc,
    )
    return BulkRejected(
        index=index,
        reason="internal_error",
        detail=f"integrity constraint violation ({constraint or sqlstate or 'unknown'})",
        external_id=external_id,
    )


def _validation_detail(exc: ValidationError) -> str:
    """A concise one-line summary of the first pydantic error (for ``schema_invalid``)."""
    first = exc.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = first.get("msg", "validation error")
    return f"{loc}: {msg}" if loc else msg

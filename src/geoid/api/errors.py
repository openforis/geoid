"""Exception handlers — DB constraint violations + service errors → HTTP.

Constraint→HTTP mapping (DB is the source of truth; switch on constraint_name):

    geoid (registry/pk) duplicate          -> 409 (fail)
    (collection_id, external_id) duplicate -> 409 (fail)
    geom_hash duplicate (catalog-wide)     -> never reaches a handler: the arbiter
                                              CTE swallows it and the service
                                              returns the incumbent geoid (201);
                                              the IntegrityError branch is only a
                                              backstop
    invalid / unsupported-type / empty geom -> 422 with ST_IsValidReason
    immutability (restrict_violation)       -> 409
    NUL byte in an input string (22021)     -> 422 "invalid characters in input (NUL)"
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

from geoid.models import (
    PK_GEOID_REGISTRY,
    PK_PLACE,
    UQ_COLLECTION_CATALOG_SLUG,
    UQ_GEOID_REGISTRY_GEOM_HASH,
    UQ_PLACE_EXTERNAL_ID,
)
from geoid.repositories._pg_errors import (
    SQLSTATE_CHARACTER_NOT_IN_REPERTOIRE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_RESTRICT_VIOLATION,
    pg_fields,
    sqlstate_of,
)
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    BulkLimitExceededError,
    CollectionForbiddenError,
    CollectionNotFoundError,
    GeometryInvalidError,
    GrantNotFoundError,
    LastOwnerGuardError,
    OwnerGrantForbiddenError,
    PlaceNotFoundError,
    PublicExternalIdLookupError,
    RegistryConsistencyError,
    WriteNotAuthorizedError,
)

logger = logging.getLogger(__name__)


def _error(status_code: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, **extra},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CollectionNotFoundError)
    async def _collection_not_found(_: Request, exc: CollectionNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), collection=exc.slug)

    @app.exception_handler(PlaceNotFoundError)
    async def _place_not_found(_: Request, exc: PlaceNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(PublicExternalIdLookupError)
    async def _public_external_id_lookup(
        _: Request, exc: PublicExternalIdLookupError
    ) -> JSONResponse:
        return _error(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(BulkLimitExceededError)
    async def _bulk_limit_exceeded(_: Request, exc: BulkLimitExceededError) -> JSONResponse:
        return _error(status.HTTP_413_CONTENT_TOO_LARGE, str(exc), count=exc.count, limit=exc.limit)

    @app.exception_handler(GeometryInvalidError)
    async def _geometry_invalid(_: Request, exc: GeometryInvalidError) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "geometry rejected (reject-don't-repair)",
            reason=exc.reason,
        )

    @app.exception_handler(AnonymousWriteForbiddenError)
    async def _anon_forbidden(_: Request, exc: AnonymousWriteForbiddenError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(WriteNotAuthorizedError)
    async def _write_not_authorized(_: Request, exc: WriteNotAuthorizedError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(CollectionForbiddenError)
    async def _collection_forbidden(_: Request, exc: CollectionForbiddenError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(OwnerGrantForbiddenError)
    async def _owner_grant_forbidden(_: Request, exc: OwnerGrantForbiddenError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc), collection=exc.slug)

    @app.exception_handler(GrantNotFoundError)
    async def _grant_not_found(_: Request, exc: GrantNotFoundError) -> JSONResponse:
        return _error(status.HTTP_404_NOT_FOUND, str(exc), collection=exc.slug, email=exc.email)

    @app.exception_handler(LastOwnerGuardError)
    async def _last_owner_guard(_: Request, exc: LastOwnerGuardError) -> JSONResponse:
        return _error(status.HTTP_409_CONFLICT, str(exc), collection=exc.slug, email=exc.email)

    @app.exception_handler(RegistryConsistencyError)
    async def _registry_consistency(_: Request, exc: RegistryConsistencyError) -> JSONResponse:
        # Should-not-happen registry/recipe drift: a dedup loser whose incumbent
        # collection never materialised. Registering a SPECIFIC-type handler (not a
        # bare Exception handler) keeps this on Starlette's ExceptionMiddleware, which
        # returns the response instead of the ServerErrorMiddleware re-raise the test
        # client trips on. logger.exception captures the traceback for ops.
        logger.exception("registry consistency error for geoid %s", exc.geoid)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal registry inconsistency",
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        constraint, sqlstate = pg_fields(exc)

        if constraint == UQ_COLLECTION_CATALOG_SLUG:
            return _error(
                status.HTTP_409_CONFLICT,
                "collection id already exists",
                constraint=constraint,
            )
        if constraint == UQ_PLACE_EXTERNAL_ID:
            return _error(
                status.HTTP_409_CONFLICT,
                "external_id already exists in this collection",
                constraint=constraint,
            )
        if constraint in (PK_GEOID_REGISTRY, PK_PLACE):
            return _error(status.HTTP_409_CONFLICT, "geoid already exists", constraint=constraint)
        if constraint == UQ_GEOID_REGISTRY_GEOM_HASH:
            # Backstop only: the arbiter CTE swallows a geom_hash clash and the
            # service returns the incumbent geoid (mint is idempotent), so this
            # constraint should never reach an error handler. Log it loudly.
            logger.error(
                "geom_hash 409 served from the IntegrityError backstop "
                "(the idempotent dedup path should have handled it)",
                exc_info=exc,
            )
            return _error(
                status.HTTP_409_CONFLICT,
                "identical geometry already exists in the catalog",
                constraint=constraint,
            )
        if sqlstate == SQLSTATE_CHECK_VIOLATION:
            return _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "geometry violates a database check (supported-type / not-empty / ST_IsValid)",
                constraint=constraint,
            )
        if sqlstate == SQLSTATE_RESTRICT_VIOLATION:
            return _error(
                status.HTTP_409_CONFLICT,
                "place is immutable; corrections mint a new geoid",
            )
        # Unrecognised integrity failure — a NOT NULL violation (23502) is a server
        # bug, and pg_fields degrading to (None, None) on a driver change lands here
        # too. Presenting either as the client's 409 conflict would mislabel it:
        # log loud (stack + Postgres DETAIL) and answer an honest 500.
        logger.error(
            "unclassified integrity violation: constraint=%r sqlstate=%r",
            constraint,
            sqlstate,
            exc_info=exc,
        )
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")

    @app.exception_handler(DBAPIError)
    async def _dbapi(_: Request, exc: DBAPIError) -> JSONResponse:
        # Registered on the BASE class so the mapping is dialect-independent
        # (asyncpg wraps 22021 as its own DataError flavour); an IntegrityError
        # still lands on its more-specific handler above via Starlette's
        # exception-class MRO walk. A NUL byte in any request string (a %00
        # path param, an escaped NUL in a body member) dies in Postgres with
        # 22021 — client input, not a server fault.
        if sqlstate_of(exc) == SQLSTATE_CHARACTER_NOT_IN_REPERTOIRE:
            return _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid characters in input (NUL)",
            )
        # Anything else keeps the loud-500 posture: re-raise so
        # ServerErrorMiddleware logs the traceback.
        raise exc

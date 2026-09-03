"""Health probe router — ``GET /health``: API liveness + DB connectivity.

Deliberately does NOT use the ``get_session`` dependency: that commits and
re-raises *outside* the handler body, so a dead DB would surface as an
unhandled 500 instead of the documented 503. The probe opens its own session,
mirroring the lifespan's tolerate-infra-errors pattern (``main.py``).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from geoid import __version__
from geoid.db import get_sessionmaker
from geoid.schemas.health import HealthStatus

logger = logging.getLogger("geoid")

router = APIRouter(tags=["health"])

# Client-side bound on the whole probe: statement_timeout is server-side and
# only applies once connected — a black-holed host can hang TCP connect far
# longer than any probe period.
_PROBE_TIMEOUT_S = 5.0

# The infra-only set the lifespan tolerates, plus pool-checkout exhaustion.
# Builtin TimeoutError (raised by asyncio.timeout) is an OSError subclass
# (3.10+), so the timeout guard is covered. Programming errors propagate as 500.
_DB_ERRORS = (OperationalError, InterfaceError, SQLAlchemyTimeoutError, OSError)


@router.get(
    "/health",
    response_model=HealthStatus,
    summary="Health probe: API process + DB connectivity",
    responses={503: {"model": HealthStatus, "description": "Database unreachable."}},
)
async def health(response: Response) -> HealthStatus:
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
    except _DB_ERRORS as exc:
        # Full message goes to the log only — asyncpg messages embed host/user.
        logger.warning("health: DB probe failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(
            status="unavailable", db="down", version=__version__, detail=type(exc).__name__
        )
    return HealthStatus(status="ok", db="up", version=__version__)

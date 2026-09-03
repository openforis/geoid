"""FastAPI application factory — router mount, OpenAPI metadata, lifespan bootstrap."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.exc import InterfaceError, OperationalError

from geoid import __version__
from geoid.api import grants, health, manage, ogc, places
from geoid.api.errors import register_exception_handlers
from geoid.config import Settings, get_settings
from geoid.db import dispose_engine, get_sessionmaker
from geoid.services.bootstrap import ensure_public_collection

logger = logging.getLogger("geoid")

_DESCRIPTION = """\
**GeoID** mints a globally unique, **immutable** identifier (a *geoid*, UUIDv8)
for every geospatial place, derived from the geometry itself — the same geometry
always returns the same geoid — and tracks provenance.

* **Mint** — `POST /items` (one GeoJSON Feature) → `{geoid, uri}`
* **Bulk mint** — `POST /items/bulk` (a GeoJSON FeatureCollection, processed
  in-request, returns a per-feature report)
* **Resolve** — `GET /{geoid}` (GeoJSON, or WKT with `?format=wkt`)

These three public operations ignore authentication until further notice.
"""


_TAGS_METADATA = [
    {"name": "registry", "description": "Write and resolution endpoints"},
]


# uvicorn binds the listener socket only after lifespan startup completes, so an
# unresponsive IdP must never stall readiness (PyJWKClient's default urllib timeout
# is 30 s — far past any acceptable cold-start budget).
_JWKS_PREFETCH_TIMEOUT_S = 5.0


async def _prefetch_jwks() -> None:
    """Warm the JWKS key-set cache so the first authenticated request after a
    cold start skips the ~200-340 ms blocking key fetch (perf F3, 2026-07-10)."""
    try:
        from geoid.deps import get_jwks_client

        jwks_client = get_jwks_client()
        if jwks_client is None:  # OIDC disabled (bare development config)
            return
        import anyio  # lazy, mirroring deps._resolve_oidc

        with anyio.fail_after(_JWKS_PREFETCH_TIMEOUT_S):
            # abandon_on_cancel: on timeout the fetch thread finishes in the
            # background (it may still populate the cache); startup moves on.
            await anyio.to_thread.run_sync(jwks_client.get_jwk_set, abandon_on_cancel=True)
        logger.info("JWKS prefetch: signing keys warmed")
    except Exception as exc:
        # Startup must never fail on a warm-up; the request path self-heals.
        logger.warning("JWKS prefetch skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            await ensure_public_collection(session, settings)
            await session.commit()
        logger.info("bootstrap: ensured public collection %r", settings.public_collection)
    except (OperationalError, InterfaceError, OSError) as exc:
        # Tolerate only a not-ready DB; let programming errors propagate.
        logger.warning("bootstrap skipped (DB not ready?): %s", exc)
    await _prefetch_jwks()
    yield
    await dispose_engine()


def _swagger_oauth2(settings: Settings) -> tuple[list, dict | None]:
    """Optional Swagger Authorization-Code+PKCE config (off by default).

    Returns ``(dependencies, init_oauth)``. When the flag is off (or OIDC is not
    enabled — the auth/token URLs derive from the issuer), both are empty/None, so
    ``FastAPI(...)`` sees its defaults and ``/openapi.json`` + ``/docs`` are
    byte-identical to today. When on, an ``OAuth2AuthorizationCodeBearer`` scheme is
    declared (``auto_error=False`` → non-blocking, NOT a hard route gate) purely so
    Swagger renders the PKCE login button; the obtained token rides the same
    Authorization header ``require_principal`` already reads.
    """
    if not (settings.swagger_oauth2_enabled and settings.oidc_enabled):
        return [], None
    from fastapi.security import OAuth2AuthorizationCodeBearer

    oauth2_scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl=settings.oidc_auth_url or "",
        tokenUrl=settings.oidc_token_url or "",
        auto_error=False,
        scheme_name="Single Sign-On",
        description=(
            "Sign in to authorize your requests. You'll be redirected to sign in, then "
            "brought back here already authorized — nothing to copy or paste."
        ),
    )
    init_oauth = {
        "clientId": settings.swagger_oauth2_client_id,
        "usePkceWithAuthorizationCodeGrant": True,
        # Deliberately absent from the scheme above: Swagger UI seeds its scope set from
        # initOAuth alone, so these ride every login without rendering scope checkboxes.
        "scopes": "openid profile email",
    }
    return [Depends(oauth2_scheme)], init_oauth


def create_app() -> FastAPI:
    settings = get_settings()
    oauth2_dependencies, swagger_init_oauth = _swagger_oauth2(settings)
    app = FastAPI(
        title="GeoID",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        root_path=settings.root_path,
        license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        contact={"name": "FAO GeoID Team"},
        openapi_tags=_TAGS_METADATA,
        dependencies=oauth2_dependencies or None,
        swagger_ui_init_oauth=swagger_init_oauth,
    )
    register_exception_handlers(app)

    # Probe surface first, then the OGC read surface, the per-collection grant
    # routes, the registry/resolver, and the admin /manage router. The grants
    # router is mounted BEFORE places so its literal /collections/... routes are
    # matched ahead of the root /{geoid} catch-all; /health is a literal path —
    # no OGC collision. Everything but `places` is hidden from /docs: the public
    # surface is the three registry operations the client fixed. Hiding is
    # cosmetic — every route stays live and keeps its existing auth gate.
    app.include_router(health.router, include_in_schema=False)
    app.include_router(ogc.router, include_in_schema=False)
    app.include_router(grants.router, include_in_schema=False)
    app.include_router(places.router)
    app.include_router(manage.router, include_in_schema=False)

    app.state.settings = settings
    return app


app = create_app()

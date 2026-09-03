"""Request-scoped dependencies: the auth seam and the DB session.

**Keycloak-only.** ``require_principal`` resolves the caller from a Keycloak RS256
JWT: no credentials → anonymous (a valid outcome; routes decide whether anonymity
is acceptable); a Bearer credential is validated against the configured realm and
its claims mapped to a :class:`Principal`; when OIDC is not enabled (development
only — a deployed environment refuses to boot without it) every credential is 401.

The OIDC validation/mapping logic lives in :mod:`geoid.auth.oidc` (pure, network-free,
unit-tested). PyJWT and ``PyJWKClient`` are imported **lazily** here so an image
built without the ``oidc`` extra still imports this module. Every rejected
credential — malformed, expired, wrong audience, JWKS outage — raises the same
byte-identical 401, so an unauthenticated caller learns nothing about the cause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from geoid import __version__
from geoid.config import Settings, get_settings

if TYPE_CHECKING:
    from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_BEARER = "Bearer"
_WWW_AUTH = {"WWW-Authenticate": _BEARER}

# auto_error=False -> anonymous requests (no header) are allowed through; routes
# decide whether anonymity is acceptable. Also renders an Authorize button in /docs;
# the pasted credential is a Keycloak access token (JWT).
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="GeoIDBearer",
    description="Paste your Keycloak access token (JWT) here to authorize your requests.",
)


@dataclass(frozen=True)
class Principal:
    """The resolved caller. Anonymous principals have ``subject is None``.

    ``email``/``email_verified`` are populated from the Keycloak claims (anonymous
    principals leave them at their defaults). They are appended after the original
    three fields so ``admin()``/``anonymous()`` and every existing call site stay
    valid. ``admin()`` is the sysadmin-tier constructor (tests and tooling).

    ``roles`` is audit/logging-only — every authorization decision branches on the
    precomputed ``is_admin`` (and the per-collection grant ladder), never on this
    tuple. Do not treat it as a security control.
    """

    subject: str | None
    is_admin: bool = False
    roles: tuple[str, ...] = field(default_factory=tuple)
    email: str | None = None
    email_verified: bool = False

    @property
    def is_anonymous(self) -> bool:
        return self.subject is None

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(subject=None, is_admin=False, roles=())

    @classmethod
    def admin(cls) -> Principal:
        return cls(subject="admin", is_admin=True, roles=("sysadmin",))


def _invalid_token() -> HTTPException:
    """The single 401 every rejected credential raises (byte-identical body)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid bearer token",
        headers=_WWW_AUTH,
    )


@lru_cache
def get_jwks_client() -> PyJWKClient | None:
    """The process-wide JWKS client (cached), or ``None`` when OIDC is disabled.

    Injected via ``Depends`` so tests override it with a fake (no network). The real
    client caches signing keys by ``kid`` in-process with a 300 s lifespan, so the
    blocking JWKS fetch happens at most once per key-rotation window. PyJWT is
    imported lazily so importing this module never requires the ``oidc`` extra.
    """
    settings = get_settings()
    if not settings.oidc_enabled:
        return None
    from jwt import PyJWKClient

    # Cloudflare 403s the default Python-urllib User-Agent on the FAO realm's JWKS
    # endpoint; any real UA unblocks it (PyJWKClient forwards headers= to urllib).
    return PyJWKClient(
        settings.oidc_jwks_url,
        cache_keys=True,
        lifespan=300,
        headers={"User-Agent": f"geoid/{__version__}"},
    )


async def _resolve_oidc(
    token: str, settings: Settings, jwks_client: PyJWKClient | None
) -> Principal:
    """Validate ``token`` as a Keycloak JWT and map its claims to a Principal.

    The (rare, cache-missing) blocking JWKS fetch is offloaded to a worker thread so
    it never stalls the event loop. Any validation/JWKS failure becomes the shared
    401; only the exception *type* is logged — never the token or the JWKS body.
    """
    import anyio
    import jwt

    from geoid.auth.oidc import decode_and_validate, principal_from_claims

    if jwks_client is None:
        # oidc_enabled but no client (misconfiguration) — treat as an invalid token,
        # never a 500: an unauthenticated caller must not learn about server state.
        # But the operator must: this branch 401s EVERY login until fixed.
        logger.error("OIDC is enabled but no JWKS client is configured; all OIDC logins 401")
        raise _invalid_token()
    try:
        claims = await anyio.to_thread.run_sync(decode_and_validate, token, settings, jwks_client)
    except jwt.PyJWKClientError as exc:
        # JWKS infrastructure failure (IdP down, Cloudflare block, bad URL) — a full
        # IdP outage must be distinguishable from one user's bad token. str(exc) is
        # server infra detail (URL/error), never the token, so it is safe to log.
        logger.error("OIDC JWKS failure: %s", exc)
        raise _invalid_token() from exc
    except jwt.PyJWTError as exc:
        logger.warning("OIDC token rejected: %s", type(exc).__name__)
        raise _invalid_token() from exc
    return principal_from_claims(claims, settings)


async def require_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    jwks_client: PyJWKClient | None = Depends(get_jwks_client),
) -> Principal:
    """Resolve the caller's identity. Anonymous is a valid (non-error) outcome.

    Dispatch: no credentials → anonymous; non-Bearer scheme → 401; when OIDC is
    enabled, validate as a Keycloak JWT; otherwise 401 (a bare development config
    is anonymous-only). ``settings``/``jwks_client`` are injected (not fetched
    directly) so the auth path honours ``app.dependency_overrides`` in tests.
    """
    return await principal_from_credentials(creds, settings, jwks_client)


async def principal_from_credentials(
    creds: HTTPAuthorizationCredentials | None,
    settings: Settings,
    jwks_client: PyJWKClient | None,
) -> Principal:
    """Resolve already-extracted HTTP credentials without declaring a dependency.

    ``require_principal`` is the normal FastAPI dependency. This lower-level seam
    exists for a conditionally authenticated route: the public collection must
    ignore even malformed credentials, while managed collection writes still use
    the exact same validation and error contract.
    """
    if creds is None:
        return Principal.anonymous()
    if creds.scheme.lower() != _BEARER.lower():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported authorization scheme",
            headers=_WWW_AUTH,
        )
    if settings.oidc_enabled:
        return await _resolve_oidc(creds.credentials, settings, jwks_client)
    raise _invalid_token()


async def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    """Gate a route on admin privileges. Anonymous -> 401, non-admin -> 403."""
    if principal.is_admin:
        return principal
    raise HTTPException(
        status_code=(
            status.HTTP_401_UNAUTHORIZED if principal.is_anonymous else status.HTTP_403_FORBIDDEN
        ),
        detail="Admin privileges required",
        headers=_WWW_AUTH,
    )

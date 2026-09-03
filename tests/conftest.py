"""Shared test fixtures.

Integration tests run against an ephemeral PostGIS container (testcontainers),
pinned to ``postgis/postgis:17-3.5`` on ``linux/amd64`` (the official image has
no arm64 manifest; native on amd64 CI, emulated on arm64 dev machines). This is
not Cloud SQL's exact GEOS build, but under identity recipe v2 (migration 0008)
that no longer matters: the geoid/dedup hash is engine-independent (integer-
lattice canonicalization, ADR-007), so the golden-vector corpus must match
bit-for-bit here AND on Cloud SQL alike (tests/integration/
test_dedup_golden_vectors.py). If Docker is unavailable, integration tests skip
cleanly so ``pytest -m unit`` still runs anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Force amd64 so the official postgis image (no arm64 manifest) runs everywhere.
os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

# Docker Desktop on macOS exposes its socket under ~/.docker/run, but docker-py
# (used by testcontainers) defaults to /var/run/docker.sock and does not read the
# CLI context. Point both at the Desktop socket when present.
if "DOCKER_HOST" not in os.environ:
    _desktop_socket = Path.home() / ".docker" / "run" / "docker.sock"
    if _desktop_socket.exists():
        os.environ["DOCKER_HOST"] = f"unix://{_desktop_socket}"

# The with-block tears the container down; ryuk (the reaper) is unnecessary and
# can be flaky against Docker Desktop.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

_TRUNCATE = (
    "TRUNCATE place, geoid_registry, change_log, collection_grant, collection, catalog "
    "RESTART IDENTITY CASCADE"
)


def _docker_available() -> bool:
    try:
        import docker
    except ImportError:
        return False  # docker package not installed
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False  # installed but daemon down / unreachable


@pytest.fixture(scope="session")
def _postgis():
    if not _docker_available():
        pytest.skip("Docker is not available; skipping PostGIS integration tests")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        "postgis/postgis:17-3.5",
        username="geoid",
        password="geoid",
        dbname="geoid",
        driver="asyncpg",
    ).with_kwargs(platform="linux/amd64")
    with container as pg:
        yield pg


@pytest.fixture(scope="session")
def _migrated(_postgis):
    """Point settings at the container, apply migrations to head, return the URL."""
    from alembic import command
    from alembic.config import Config

    from geoid.config import get_settings

    url = _postgis.get_connection_url()
    os.environ["GEOID_DATABASE_URL"] = url
    # The default app is OIDC-enabled (Keycloak is the only auth path); the JWKS
    # fetch is faked per-fixture via the get_jwks_client dependency override.
    os.environ["GEOID_OIDC_ISSUER"] = _OIDC_ISSUER
    os.environ["GEOID_OIDC_JWKS_URL"] = "https://idp.test/jwks"
    os.environ["GEOID_BASE_URL"] = "http://testserver"
    os.environ["GEOID_INSTANCE_ID"] = "test-instance"
    os.environ["GEOID_PUBLIC_COLLECTION"] = "public"
    get_settings.cache_clear()

    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def db_clean(_migrated):
    """A clean schema with the reserved ``public`` collection, fresh per test."""
    from sqlalchemy import text

    from geoid.config import get_settings
    from geoid.db import dispose_engine, get_sessionmaker
    from geoid.services.bootstrap import ensure_public_collection

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # The immutability / append-only triggers now block TRUNCATE too. The test
        # role is a superuser, so disable triggers for the reset only (replica mode)
        # and restore immediately — a production app role cannot do this.
        await session.execute(text("SET session_replication_role = replica"))
        await session.execute(text(_TRUNCATE))
        await session.execute(text("SET session_replication_role = origin"))
        public = await ensure_public_collection(session, get_settings())
        # Migration 0012's partial-index predicate pins the public collection
        # UUID minted at migrate time; the TRUNCATE above re-minted the row, so
        # re-aim the predicate at the fresh UUID (the same recovery SQL an
        # operator runs after a teardown — the documented stale-predicate
        # ceiling in 0012).
        await session.execute(text("DROP INDEX IF EXISTS uq_place_collection_external_id"))
        await session.execute(
            text(
                "CREATE UNIQUE INDEX uq_place_collection_external_id "
                "ON place (collection_id, external_id) "
                f"WHERE collection_id <> '{public.id}'"
            )
        )
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
async def session(db_clean):
    """A raw async DB session (for repository / SQL-recipe tests)."""
    from geoid.db import get_sessionmaker

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
async def client(db_clean, oidc_keypair):
    """An httpx AsyncClient bound to a freshly built app (in-process ASGI).

    The app reads the OIDC-enabled env from ``_migrated``; ``get_jwks_client`` is
    overridden with a fake returning the in-test public key, so synthetic Keycloak
    JWTs (``make_token``) validate without any network.
    """
    from httpx import ASGITransport, AsyncClient

    from geoid.deps import get_jwks_client
    from geoid.main import create_app

    app = create_app()
    app.dependency_overrides[get_jwks_client] = lambda: _fake_jwks(oidc_keypair)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def admin_headers(make_token) -> dict[str, str]:
    """A sysadmin credential: a synthetic Keycloak JWT carrying ``geoid.sysadmin``."""
    token = make_token(sub="kc-sysadmin", email="sysadmin@fao.org", roles=("geoid.sysadmin",))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ext_collection(client, admin_headers) -> str:
    """An open-write managed collection where external_id uniqueness applies.

    The reserved ``public`` collection is excluded from the unique index
    (migration 0012) and its external-id lookup answers 400 — tests pinning
    the 409/resolver behavior mint here instead. ``public_write=True`` keeps
    the anonymous-mint test pattern working.
    """
    resp = await client.post(
        "/manage/collections",
        headers=admin_headers,
        json={"id": "extcol", "public_write": True},
    )
    assert resp.status_code == 201, resp.text
    return "extcol"


# --- OIDC / Keycloak test infra (synthetic RS256 tokens, fake JWKS, no network) -

_OIDC_ISSUER = "https://idp.test/realms/geoid"


def _fake_jwks(oidc_keypair):
    """A stand-in JWKS client returning the in-test public key for any token."""
    from types import SimpleNamespace

    _, public_key = oidc_keypair
    return SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key))


@pytest.fixture(scope="session")
def oidc_keypair():
    """One RSA keypair for the whole OIDC integration run (key-gen is slow)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def make_token(oidc_keypair):
    """Factory minting a synthetic Keycloak RS256 JWT signed by the in-test key."""
    import time

    import jwt

    private_key, _ = oidc_keypair

    def _make(
        *,
        sub="kc-user",
        email=None,
        email_verified=True,
        roles=(),
        aud="geoid-be",
        iss=_OIDC_ISSUER,
        exp_delta=300,
    ) -> str:
        now = int(time.time())
        claims = {"sub": sub, "iss": iss, "aud": aud, "exp": now + exp_delta, "iat": now}
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = email_verified
        if roles:
            claims["resource_access"] = {"geoid-roles": {"roles": list(roles)}}
        return jwt.encode(claims, private_key, algorithm="RS256")

    return _make


@pytest.fixture
def bearer():
    """Build an Authorization header from a token string."""
    return lambda token: {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def oidc_client(db_clean, oidc_keypair):
    """An httpx client bound to an app with OIDC settings pinned explicitly.

    ``get_settings`` is overridden only for request-time dependency injection (the DB
    engine keeps using the real testcontainer settings), and ``get_jwks_client`` is
    overridden with a fake that returns the in-test public key for any token.
    Equivalent to the default ``client`` (which reads the same OIDC env) — kept for
    the auth suites that pin the settings→dependency wiring itself.
    """
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.deps import get_jwks_client
    from geoid.main import create_app

    settings = Settings(
        _env_file=None, oidc_issuer=_OIDC_ISSUER, oidc_jwks_url="https://idp.test/jwks"
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_jwks_client] = lambda: _fake_jwks(oidc_keypair)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
async def cache_client(db_clean, oidc_keypair):
    """An ``oidc_client`` twin with the resolver HTTP-cache trial on (3600s).

    Same settings-override / fake-JWKS wiring, so it serves BOTH anonymous and
    bearer requests; only ``resolver_cache_max_age`` differs from the default 0.
    """
    from httpx import ASGITransport, AsyncClient

    from geoid.config import Settings, get_settings
    from geoid.deps import get_jwks_client
    from geoid.main import create_app

    settings = Settings(
        _env_file=None,
        oidc_issuer=_OIDC_ISSUER,
        oidc_jwks_url="https://idp.test/jwks",
        resolver_cache_max_age=3600,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_jwks_client] = lambda: _fake_jwks(oidc_keypair)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


# --- Shared geometry fixtures (GeoJSON Features) ----------------------------


def _feature(coords, *, external_id=None, properties=None, geom_type="Polygon"):
    feature = {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": properties or {},
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


@pytest.fixture
def unit_square_ccw():
    # CCW unit square at origin offset (10,10)
    return _feature([[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]])


@pytest.fixture
def unit_square_reversed():
    # Same square, CW winding + different ring start -> same canonical hash
    return _feature([[[10, 10], [10, 11], [11, 11], [11, 10], [10, 10]]])


@pytest.fixture
def other_square():
    return _feature([[[30, 30], [31, 30], [31, 31], [30, 31], [30, 30]]])

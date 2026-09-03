"""Environment-driven configuration (pydantic-settings, ``GEOID_*`` prefix).

The core service imports no cloud SDK; every deployment difference (FAO GCP vs
on-prem docker-compose vs a standalone country instance) is expressed here as
configuration, so the *same image* serves all of them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """DB-layer configuration — the full surface the migrate job needs."""

    # extra="ignore" is load-bearing: a full .env with app-level GEOID_* keys
    # must not trip extra-field validation here.
    model_config = SettingsConfigDict(
        env_prefix="GEOID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+asyncpg://geoid:geoid@localhost:5432/geoid",
        description="Async SQLAlchemy URL (asyncpg driver).",
    )

    # --- Connection pool + server-side timeouts (bound the blast radius of a
    #     slow client holding a pooled connection) ------------------------------
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1, description="seconds to wait for a pooled conn")
    db_statement_timeout_ms: int = Field(default=30_000, ge=0, description="0 disables")
    db_idle_in_tx_timeout_ms: int = Field(default=60_000, ge=0, description="0 disables")


class Settings(DatabaseSettings):
    """All runtime configuration. Read once and cached via :func:`get_settings`."""

    # --- Deployment ---------------------------------------------------------
    environment: Literal["development", "review", "production"] = Field(
        default="development",
        description=(
            "Deployment mode (env: GEOID_ENVIRONMENT). Outside development OIDC "
            "must be configured or the service refuses to boot — see the validator. "
            "'review' is a deployed pre-production env: it enforces the OIDC-required "
            "guard exactly like 'production'."
        ),
    )

    # --- Identity / resolvability ------------------------------------------
    base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL used to derive URIs and OGC links on read.",
    )
    root_path: str = Field(
        default="",
        description=(
            "Sub-path the API is mounted under behind a reverse proxy (env: "
            "GEOID_ROOT_PATH), e.g. '/geoid'. Passed to FastAPI(root_path=...) so "
            "Swagger/OpenAPI resolve behind the proxy, and prepended to BASE_URL when "
            "deriving public URIs/OGC links. The proxy is expected to strip this prefix "
            "before forwarding."
        ),
    )

    # --- Data-layer anonymous / federation knobs ---------------------------
    # Same format rule as CollectionCreate.id — bootstrap inserts this slug
    # without passing through the API validator, so the guard lives here too.
    public_collection: str = Field(
        default="public",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Reserved collection slug that accepts anonymous writes.",
    )
    instance_id: str = Field(
        default="fao-central",
        description="This instance's id, stamped into provenance + originating_instance.",
    )

    # --- Geometry identity precision ----------------------------------------
    dedup_grid_default: float = Field(
        default=1e-7,
        gt=0,
        description=(
            "The ONE global identity-lattice cell size (decimal degrees) for "
            "geometry dedup/identity. 1e-7 ≈ 1cm/vertex — exact-match semantics "
            "(float-jitter immunity only). "
            "Documented canonical value: the app never passes it to the DB — recipe "
            "v2 quantizes by the integer SCALE = 10^7 pinned inside migration 0008's "
            "geoid_quantize_v2() (and mirrored by domain/geometry_identity.SCALE); a "
            "unit test pins SCALE == round(1 / this value), so retunes are migration "
            "events (identity-version events under v2), never a config-only change."
        ),
    )

    # --- Bulk write (synchronous multi-geometry POST) ----------------------
    bulk_max_features: int = Field(
        default=1_000,
        ge=1,
        description=(
            "Hard cap on the number of features accepted in one bulk POST "
            "FeatureCollection. A write bound MUST error (413), never silently "
            "truncate — exceeding it rejects the whole request."
        ),
    )

    # --- Resolver HTTP-cache trial ------------------------------------------
    resolver_cache_max_age: int = Field(
        default=0,
        ge=0,
        description=(
            "HTTP-caching trial on the geoid and external-id resolvers (env: "
            "GEOID_RESOLVER_CACHE_MAX_AGE), in seconds. 0 (default) = fully off — "
            "responses stay byte-identical. > 0: public representations carry a "
            "strong per-representation ETag + Cache-Control: public, max-age=<n> + "
            "Vary and answer a matching If-None-Match with 304. The geoid resolver "
            "ignores authentication; authenticated external-id responses are marked "
            "private, no-store (no ETag, never a 304)."
        ),
    )

    # --- OIDC resource-server auth (Keycloak) — the ONLY authentication path.
    #     oidc_enabled gates on issuer + jwks_url. Unset (development only) the
    #     service is anonymous-only: every bearer credential is rejected with 401.
    #     Outside development both MUST be set or the service refuses to boot
    #     (see _require_oidc_outside_development). ----------------------------
    oidc_issuer: str | None = Field(
        default=None,
        description="OIDC issuer (the realm URL); auth/token endpoints derive from it.",
    )
    oidc_jwks_url: str | None = Field(
        default=None,
        description="JWKS URI; PyJWKClient caches signing keys by kid in-process.",
    )
    oidc_audience: str = Field(
        default="geoid-be",
        description=(
            "Required audience: a valid Keycloak token must carry this in `aud` (our "
            "API's client id, geoid-be). Validated only when OIDC is enabled; setting "
            "it does NOT by itself enable OIDC."
        ),
    )
    oidc_roles_client: str = Field(
        default="geoid-roles",
        description="resource_access client whose `roles` array carries GeoID roles.",
    )
    oidc_admin_role: str = Field(
        default="geoid.sysadmin",
        description="Role (under oidc_roles_client) granting the global sysadmin tier.",
    )
    oidc_leeway_seconds: int = Field(
        default=30, ge=0, description="Clock-skew leeway (seconds) for exp/nbf validation."
    )

    # --- Swagger OAuth2 (Authorization Code + PKCE) — OFF by default. The paste-a-
    #     bearer button is always present (it carries a Keycloak JWT); this flag only
    #     adds the interactive login flow, gated so /openapi.json and /docs are
    #     byte-identical to today when it is off.
    swagger_oauth2_enabled: bool = Field(default=False)
    swagger_oauth2_client_id: str = Field(default="geoid-fe")

    @model_validator(mode="after")
    def _normalize_root_path(self) -> Settings:
        # Accept "geoid", "/geoid", "/geoid/" -> "/geoid"; "" stays "".
        rp = self.root_path.strip()
        object.__setattr__(self, "root_path", "/" + rp.strip("/") if rp else "")
        return self

    @model_validator(mode="after")
    def _require_oidc_outside_development(self) -> Settings:
        # Fail fast so a missing OIDC env var can never leave a deployed environment
        # with zero authentication paths (Keycloak is the only credential validator).
        if self.environment != "development" and not self.oidc_enabled:
            raise ValueError(
                f"OIDC is not configured in {self.environment!r}; set GEOID_OIDC_ISSUER "
                "and GEOID_OIDC_JWKS_URL (Keycloak is the only authentication path; "
                "running without it is only allowed when GEOID_ENVIRONMENT=development)."
            )
        return self

    @property
    def base_url_clean(self) -> str:
        """Public base for link derivation: BASE_URL (no trailing slash) + ROOT_PATH.

        When the API sits behind a proxy sub-path (root_path, e.g. '/geoid'), every
        minted URI / OGC link must carry that prefix to stay resolvable. root_path is
        already normalised to '' or '/<path>', so this concatenation is safe.
        """
        return self.base_url.rstrip("/") + self.root_path

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_jwks_url)

    @property
    def oidc_auth_url(self) -> str | None:
        """Keycloak authorization endpoint derived from the issuer (None if unset)."""
        if not self.oidc_issuer:
            return None
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/auth"

    @property
    def oidc_token_url(self) -> str | None:
        """Keycloak token endpoint derived from the issuer (None if unset)."""
        if not self.oidc_issuer:
            return None
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Override in tests via ``get_settings.cache_clear()``."""
    return Settings()

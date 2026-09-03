"""Unit tests for configuration derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.config import DatabaseSettings, Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Integration fixtures export GEOID_* into os.environ; clear the ones these
    # derivation tests assert on so unit tests are order-independent.
    for key in (
        "GEOID_DATABASE_URL",
        "GEOID_BASE_URL",
        "GEOID_OIDC_ISSUER",
        "GEOID_OIDC_JWKS_URL",
        "GEOID_OIDC_AUDIENCE",
        "GEOID_DEDUP_GRID_DEFAULT",
        "GEOID_ENVIRONMENT",
        "GEOID_ROOT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's local .env never leaks into the assertions.
    return Settings(_env_file=None, **overrides)


def test_base_url_clean_strips_trailing_slash():
    settings = _settings(base_url="https://data.fao.org/")
    assert settings.base_url_clean == "https://data.fao.org"


def test_root_path_defaults_empty_and_leaves_base_url_clean_unchanged():
    settings = _settings(base_url="https://data.fao.org")
    assert settings.root_path == ""
    assert settings.base_url_clean == "https://data.fao.org"


@pytest.mark.parametrize("given", ["geoid", "/geoid", "/geoid/", "  /geoid  "])
def test_root_path_is_normalised_to_leading_slash_no_trailing(given):
    assert _settings(root_path=given).root_path == "/geoid"


def test_base_url_clean_includes_root_path():
    # The minted URI/OGC-link prefix must carry the proxy sub-path to stay resolvable.
    settings = _settings(base_url="https://data.example.org", root_path="/geoid")
    assert settings.base_url_clean == "https://data.example.org/geoid"


def test_oidc_disabled_without_issuer():
    assert _settings().oidc_enabled is False


def test_oidc_enabled_with_issuer_and_jwks():
    settings = _settings(
        oidc_issuer="https://idp.fao.org", oidc_jwks_url="https://idp.fao.org/jwks"
    )
    assert settings.oidc_enabled is True


def test_oidc_audience_default_is_geoid_be():
    # Confirmed from the live realm: our API's audience is geoid-be.
    assert _settings().oidc_audience == "geoid-be"


def test_oidc_role_defaults():
    s = _settings()
    assert s.oidc_roles_client == "geoid-roles"
    assert s.oidc_admin_role == "geoid.sysadmin"
    assert s.oidc_leeway_seconds == 30


def test_setting_audience_alone_does_not_enable_oidc():
    # Changing the audience default must NOT widen oidc_enabled (issuer+jwks gate it).
    assert _settings(oidc_audience="geoid-be").oidc_enabled is False


def test_oidc_auth_and_token_urls_none_without_issuer():
    s = _settings()
    assert s.oidc_auth_url is None
    assert s.oidc_token_url is None


def test_oidc_auth_and_token_urls_derive_from_issuer():
    s = _settings(oidc_issuer="https://idp.fao.org/realms/geoid/")
    assert s.oidc_auth_url == "https://idp.fao.org/realms/geoid/protocol/openid-connect/auth"
    assert s.oidc_token_url == "https://idp.fao.org/realms/geoid/protocol/openid-connect/token"


def test_swagger_oauth2_defaults_off():
    s = _settings()
    assert s.swagger_oauth2_enabled is False
    assert s.swagger_oauth2_client_id == "geoid-fe"


def test_dedup_grid_default_is_1cm():
    # Exact-match ruling: 1e-7 deg/vertex ≈ 1 cm — float-jitter immunity only.
    assert _settings().dedup_grid_default == 1e-7


def test_dedup_grid_default_env_override(monkeypatch):
    monkeypatch.setenv("GEOID_DEDUP_GRID_DEFAULT", "0.0005")
    assert _settings().dedup_grid_default == 0.0005


def test_bulk_max_features_default_is_1000():
    # The synchronous bulk POST is sized for hundreds-to-a-few-thousand geometries.
    assert _settings().bulk_max_features == 1_000


def test_resolver_cache_max_age_defaults_to_off():
    # 0 = the resolver HTTP-cache trial is fully off (responses byte-identical).
    assert _settings().resolver_cache_max_age == 0


@pytest.mark.parametrize("environment", ["review", "production"])
def test_missing_oidc_outside_development_is_rejected(environment):
    # A deployed environment must never boot with zero authentication paths.
    with pytest.raises(ValidationError):
        _settings(environment=environment)


@pytest.mark.parametrize("environment", ["review", "production"])
def test_issuer_alone_outside_development_is_rejected(environment):
    # Half-configured OIDC (issuer without JWKS URL) leaves oidc_enabled false.
    with pytest.raises(ValidationError):
        _settings(environment=environment, oidc_issuer="https://idp.fao.org/realms/geoid")


@pytest.mark.parametrize("environment", ["review", "production"])
def test_configured_oidc_outside_development_is_allowed(environment):
    settings = _settings(
        environment=environment,
        oidc_issuer="https://idp.fao.org/realms/geoid",
        oidc_jwks_url="https://idp.fao.org/realms/geoid/protocol/openid-connect/certs",
    )
    assert settings.environment == environment
    assert settings.oidc_enabled is True


def test_development_needs_no_oidc():
    settings = _settings()
    assert settings.environment == "development"
    assert settings.oidc_enabled is False


@pytest.mark.parametrize("environment", ["review", "production"])
def test_database_settings_needs_no_oidc_outside_development(monkeypatch, environment):
    # The regression pin: the migrate job constructs DatabaseSettings with no
    # OIDC config mounted; that must never trip the OIDC-required guard.
    monkeypatch.setenv("GEOID_ENVIRONMENT", environment)

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url


def test_database_settings_reads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("GEOID_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/geoid")

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/geoid"


def test_database_settings_excludes_app_level_fields():
    # Structural pin: app-layer config must not creep onto the DB-layer surface.
    for field in (
        "oidc_issuer",
        "environment",
        "base_url",
        "bulk_max_features",
        "resolver_cache_max_age",
    ):
        assert field not in DatabaseSettings.model_fields


def test_settings_inherits_database_fields():
    assert _settings().db_pool_size == 10

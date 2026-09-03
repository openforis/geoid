"""Unit contracts on ogc_service: the single feature's links + WKT shaping, and
the trimmed conformance set (the listing/filtering/queryables surface is gone)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from geoid.config import Settings
from geoid.services import ogc_service

pytestmark = pytest.mark.unit

_GEOJSON_TEXT = '{"type":"Polygon","coordinates":[[[10,10],[11,10],[11,11],[10,11],[10,10]]]}'


def _item_row() -> dict:
    return {
        "geoid": uuid.UUID("019e0000-0000-7000-8000-000000000001"),
        "geometry": _GEOJSON_TEXT,
        "created_at": datetime(2026, 6, 17, tzinfo=UTC),
        "collection_slug": "public",
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Integration fixtures export GEOID_* into os.environ; clear what these
    # assertions depend on so unit tests are order-independent.
    for key in ("GEOID_ENVIRONMENT", "GEOID_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- the single feature's self link is the durable resolver ------------------


def test_build_feature_self_link_is_the_resolver():
    feature = ogc_service.build_feature(_settings(), _item_row())
    self_link = next(link for link in feature.links if link.rel == "self")
    assert self_link.href.endswith("/019e0000-0000-7000-8000-000000000001")
    assert "/items/" not in self_link.href
    assert self_link.title == "GeoJSON"


def _wkt_alternate(links) -> object | None:
    return next(
        (link for link in links if link.rel == "alternate" and link.type == "text/plain"), None
    )


def test_build_feature_advertises_wkt_alternate():
    feature = ogc_service.build_feature(_settings(), _item_row())
    alt = _wkt_alternate(feature.links)
    assert alt is not None
    assert alt.href.endswith("/019e0000-0000-7000-8000-000000000001?f=wkt")
    assert alt.title == "WKT"


def test_feature_to_wkt_returns_valid_wkt():
    feature = ogc_service.build_feature(_settings(), _item_row())
    assert ogc_service.feature_to_wkt(feature) == "POLYGON ((10 10, 11 10, 11 11, 10 11, 10 10))"


def test_feature_to_wkt_handles_null_geometry():
    feature = ogc_service.build_feature(_settings(), {**_item_row(), "geometry": None})
    assert ogc_service.feature_to_wkt(feature) == ""


# --- the masked (non-member) feature body: bare minimum, nothing else ----------


def _rich_row() -> dict:
    return {
        **_item_row(),
        "external_id": "ext-9",
        "provenance": {
            "schema": "geoid-prov/0.2",
            "created_by": "kc-1",
            "originating_instance": "test-instance",
        },
        "originating_instance": "test-instance",
        "collection_id": uuid.UUID("019e0000-0000-7000-8000-00000000000c"),
    }


def test_masked_feature_properties_are_exactly_geoid_and_uri():
    feature = ogc_service.build_feature(_settings(), _rich_row(), full=False)
    assert set(feature.properties) == {"geoid", "uri"}
    assert feature.properties["geoid"] == "019e0000-0000-7000-8000-000000000001"
    assert feature.properties["uri"].endswith("/019e0000-0000-7000-8000-000000000001")


def test_masked_feature_links_are_exactly_self_and_wkt_alternate():
    feature = ogc_service.build_feature(_settings(), _rich_row(), full=False)
    assert [link.rel for link in feature.links] == ["self", "alternate"]
    assert _wkt_alternate(feature.links) is not None


def test_masked_feature_keeps_the_bare_geometry():
    feature = ogc_service.build_feature(_settings(), _rich_row(), full=False)
    assert feature.geometry is not None
    assert ogc_service.feature_to_wkt(feature).startswith("POLYGON")


def test_full_feature_is_byte_identical_to_the_default():
    settings = _settings()
    default = ogc_service.build_feature(settings, _rich_row())
    explicit = ogc_service.build_feature(settings, _rich_row(), full=True)
    assert default == explicit
    assert default.properties["external_id"] == "ext-9"
    assert default.properties["_geoid_provenance"]["created_by"] == "kc-1"


def test_full_feature_properties_are_only_server_metadata():
    feature = ogc_service.build_feature(_settings(), _rich_row())
    assert set(feature.properties) == {
        "geoid",
        "uri",
        "external_id",
        "created_at",
        "originating_instance",
        "_geoid_provenance",
    }
    assert feature.properties["_geoid_provenance"] == {
        "schema": "geoid-prov/0.2",
        "created_by": "kc-1",
        "originating_instance": "test-instance",
    }


def test_conformance_keeps_core_drops_filter_and_cql2():
    # The item read surface (listing/filtering/queryables) was removed, so Part 3
    # (filter/queryables) and CQL2 are no longer advertised. Core/OAS30/GeoJSON stay.
    classes = ogc_service.CONFORMANCE_CLASSES
    assert len(classes) == 3
    joined = " ".join(classes).lower()
    assert "conf/core" in joined
    assert "oas30" in joined
    assert "geojson" in joined
    assert "cql2" not in joined
    assert "filter" not in joined
    assert "queryables" not in joined
    assert "wkt" not in joined

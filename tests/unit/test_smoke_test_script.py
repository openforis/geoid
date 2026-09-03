"""Unit tests for the post-deploy smoke script's write-gate validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    scripts = ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("smoke_test", scripts / "smoke_test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


smoke_test = _load_script_module()


def _response(
    body: bytes,
    *,
    status: int = 201,
    location: str = "https://example.test/geoid/abc",
    path: str = "/items",
) -> httpx.Response:
    return httpx.Response(
        status,
        content=body,
        headers={"content-type": "application/json", "Location": location},
        request=httpx.Request("POST", f"https://example.test{path}"),
    )


def test_sentinel_paths_cover_public_aliases_and_scoped_repeat():
    assert smoke_test._sentinel_paths("public", "public") == (
        "/items",
        "/collections/public/items",
        "/items/bulk",
        "/collections/public/items/bulk",
    )


def test_sentinel_paths_use_scoped_routes_for_managed_collection():
    assert smoke_test._sentinel_paths("managed", "public") == (
        "/collections/managed/items",
        "/collections/managed/items",
        "/collections/managed/items/bulk",
        "/collections/managed/items/bulk",
    )


def test_bulk_alias_parity_requires_identical_status_bytes_and_media_type():
    raw = b'{"summary":{"received":1,"accepted":1,"rejected":0},"accepted":[],"rejected":[]}'
    first = _response(raw, status=200, path="/items/bulk")
    second = _response(raw, status=200, path="/collections/public/items/bulk")

    smoke_test._validate_bulk_alias_parity(first, second)

    reordered = _response(
        b'{"accepted":[],"summary":{"received":1,"accepted":1,"rejected":0},"rejected":[]}',
        status=200,
    )
    with pytest.raises(smoke_test.CheckFailed, match="raw body diverged"):
        smoke_test._validate_bulk_alias_parity(first, reordered)

    other_status = _response(raw, status=201)
    with pytest.raises(smoke_test.CheckFailed, match="status diverged"):
        smoke_test._validate_bulk_alias_parity(first, other_status)


def test_single_mint_validation_requires_raw_body_and_matching_location():
    raw = b'{"geoid":"abc","uri":"https://example.test/geoid/abc","external_id":null}'
    first = _response(raw)
    second = _response(raw, path="/collections/public/items")

    assert smoke_test._validate_single_mints(first, second)["geoid"] == "abc"

    reordered = _response(
        b'{"uri":"https://example.test/geoid/abc","geoid":"abc","external_id":null}'
    )
    with pytest.raises(smoke_test.CheckFailed, match="raw body diverged"):
        smoke_test._validate_single_mints(first, reordered)

    wrong_location = _response(raw, location="https://example.test/geoid/other")
    with pytest.raises(smoke_test.CheckFailed, match="Location diverged"):
        smoke_test._validate_single_mints(first, wrong_location)


def test_bulk_mint_validation_requires_one_accepted_matching_geoid():
    good = _response(
        b'{"summary":{"received":1,"accepted":1,"rejected":0},'
        b'"accepted":[{"index":0,"geoid":"abc"}],"rejected":[]}',
        status=200,
        path="/items/bulk",
    )
    smoke_test._validate_bulk_mint(good, "abc")

    wrong_geoid = _response(
        b'{"summary":{"received":1,"accepted":1,"rejected":0},'
        b'"accepted":[{"index":0,"geoid":"other"}],"rejected":[]}',
        status=200,
        path="/items/bulk",
    )
    with pytest.raises(smoke_test.CheckFailed, match="bulk sentinel geoid diverged"):
        smoke_test._validate_bulk_mint(wrong_geoid, "abc")

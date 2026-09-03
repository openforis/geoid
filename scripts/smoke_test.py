#!/usr/bin/env python3
"""Post-deploy smoke test — verify a deployed GeoID instance end-to-end.

Read-only by default, so it is safe against production (places are append-only;
nothing is minted unless asked). ``--mint`` adds a three-operation write probe
that submits ONE fixed sentinel through the public and collection-scoped single
routes, then through bulk. The mint is idempotent: both single submissions answer
**201 with byte-identical bodies and matching Location headers**, and bulk accepts
the same geoid without creating another row. At most one permanent row per
catalog, ever.

    uv run python scripts/smoke_test.py            # read-only checks
    uv run python scripts/smoke_test.py --mint     # + single/scoped/bulk write probe

Env:
    GEOID_BASE_URL     default http://localhost:8000 — the URL under test;
                       every returned link/uri must carry its host (catches the
                       BASE_URL misconfiguration, the #1 launch risk)
    GEOID_EXPECTED_LINK_BASE
                       optional; the public base links must carry when it differs
                       from the URL under test (smoking the direct *.run.app URL)
    GEOID_COLLECTION   default public
    GEOID_BEARER_TOKEN a valid Keycloak access token (JWT). One carrying the
                       sysadmin role enables the admin-gated collection read checks
                       (GET /collections, GET /collections/{id}) and minting into a
                       managed (non-anon) collection. Without it those read checks
                       are skipped.

Exit codes: 0 all checks passed · 1 one or more failed (CI / Cloud Run job friendly)
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from _timing import print_timings, record

BASE = os.environ.get("GEOID_BASE_URL", "http://localhost:8000").rstrip("/")
# Links/uris are minted under the server's configured public base, which can differ
# from the URL under test (e.g. smoking the direct *.run.app URL behind the LB).
EXPECTED_LINK_BASE = os.environ.get("GEOID_EXPECTED_LINK_BASE", "").rstrip("/") or BASE
COLLECTION = os.environ.get("GEOID_COLLECTION", "public")
# The server's reserved public collection: its external_id values are stored
# but neither unique nor resolvable (migration 0012), so the resolve probe
# expects the explicit 400 there.
PUBLIC_COLLECTION = os.environ.get("GEOID_PUBLIC_COLLECTION", "public")
BEARER_TOKEN = os.environ.get("GEOID_BEARER_TOKEN")

SENTINEL_EXTERNAL_ID = "geoid-smoke-sentinel"
# Fixed tiny (~11 m) square in the Gulf of Guinea. Constant on purpose: the
# exact-match dedup recipe maps every re-run onto the same incumbent geoid.
SENTINEL_FEATURE = {
    "type": "Feature",
    "id": SENTINEL_EXTERNAL_ID,
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [0.0001, 0.0], [0.0001, 0.0001], [0.0, 0.0001], [0.0, 0.0]]],
    },
    "properties": {"name": "GeoID smoke-test sentinel", "purpose": "post-deploy verification"},
}


class CheckFailed(Exception):
    """A smoke check failed. Raised instead of ``assert`` so the deploy gate

    survives ``python -O`` / ``PYTHONOPTIMIZE`` (which strip asserts into
    silent success).
    """


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BEARER_TOKEN}"} if BEARER_TOKEN else {}


def _expected_netloc() -> str:
    return urlsplit(EXPECTED_LINK_BASE).netloc


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    expect_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    resp = record(f"GET {path}", client.get(f"{BASE}{path}", headers=headers or {}))
    _ensure(resp.status_code == 200, f"GET {path} → {resp.status_code}: {resp.text[:200]}")
    if expect_type:
        ctype = resp.headers.get("content-type", "")
        _ensure(
            ctype.startswith(expect_type),
            f"GET {path} content-type {ctype!r}, expected {expect_type!r}",
        )
    return resp.json()


# --- Read checks (safe against production) -----------------------------------


def check_landing(client: httpx.Client) -> str:
    body = _get_json(client, "/")
    links = body.get("links") or []
    _ensure(bool(links), "landing page advertises no links")
    expected = _expected_netloc()
    wrong = [link["href"] for link in links if urlsplit(link["href"]).netloc != expected]
    _ensure(
        not wrong,
        f"links not on {expected!r}: {wrong} — is GEOID_BASE_URL misconfigured on the server?",
    )
    return f"{len(links)} links, all on {expected!r}"


def check_conformance(client: httpx.Client) -> str:
    classes = _get_json(client, "/conformance").get("conformsTo") or []
    _ensure(bool(classes), "empty conformsTo")
    return f"{len(classes)} conformance classes"


def check_collections(client: httpx.Client) -> str:
    # /collections is admin-gated — send the bearer token.
    ids = [
        c.get("id")
        for c in _get_json(client, "/collections", headers=_headers()).get("collections", [])
    ]
    _ensure(COLLECTION in ids, f"collection {COLLECTION!r} not in {ids}")
    return f"{len(ids)} collections, {COLLECTION!r} present"


def check_collection_desc(client: httpx.Client) -> str:
    body = _get_json(client, f"/collections/{COLLECTION}", headers=_headers())
    _ensure(body.get("id") == COLLECTION, f"unexpected collection id {body.get('id')!r}")
    return f"id={body['id']!r}"


# Public read checks (no token, safe against production).
READ_CHECKS: tuple[tuple[str, Callable[[httpx.Client], str]], ...] = (
    ("landing links", check_landing),
    ("conformance", check_conformance),
)

# Admin-gated read checks — only run when GEOID_BEARER_TOKEN is set.
ADMIN_READ_CHECKS: tuple[tuple[str, Callable[[httpx.Client], str]], ...] = (
    ("collections list", check_collections),
    ("collection describe", check_collection_desc),
)


# --- Write probe (--mint, idempotent) ----------------------------------------


def _sentinel_paths(collection: str, public_collection: str) -> tuple[str, str, str, str]:
    """(single, single repeat, bulk, bulk repeat) — the public paths pair with the
    scoped ones only when this run targets the public collection."""
    scoped = f"/collections/{collection}/items"
    if collection == public_collection:
        return "/items", scoped, "/items/bulk", f"{scoped}/bulk"
    return scoped, scoped, f"{scoped}/bulk", f"{scoped}/bulk"


def _post_sentinel(client: httpx.Client, path: str, label: str) -> httpx.Response:
    """Submit the fixed sentinel and retain the raw response for byte comparison."""
    resp = record(
        label,
        client.post(f"{BASE}{path}", json=SENTINEL_FEATURE, headers=_headers()),
    )
    if resp.status_code == 409:
        raise CheckFailed(
            "409 external_id conflict — the sentinel external_id exists with a "
            "DIFFERENT geometry; was SENTINEL_FEATURE changed since the first run?"
        )
    _ensure(resp.status_code == 201, f"{path} → {resp.status_code}: {resp.text[:200]}")
    return resp


def _validate_single_mints(first: httpx.Response, second: httpx.Response) -> dict:
    first_body = first.json()
    second_body = second.json()
    first_geoid = first_body.get("geoid")
    second_geoid = second_body.get("geoid")
    _ensure(bool(first_geoid), f"first mint response has no geoid: {first.text[:200]}")
    _ensure(
        second_geoid == first_geoid,
        f"repeat mint geoid diverged: {second_geoid!r} != {first_geoid!r}",
    )
    _ensure(
        second.content == first.content,
        "repeat mint raw body diverged — decoded JSON equality is insufficient",
    )
    first_location = first.headers.get("Location")
    second_location = second.headers.get("Location")
    _ensure(bool(first_location), "first mint response has no Location header")
    _ensure(
        second_location == first_location,
        f"repeat mint Location diverged: {second_location!r} != {first_location!r}",
    )
    return first_body


def _validate_bulk_mint(resp: httpx.Response, geoid: str) -> None:
    _ensure(
        resp.status_code == 200, f"{resp.request.url.path} → {resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    _ensure(
        body.get("summary") == {"received": 1, "accepted": 1, "rejected": 0},
        f"bulk sentinel summary is not one accepted/no rejects: {body.get('summary')!r}",
    )
    accepted = body.get("accepted")
    rejected = body.get("rejected")
    _ensure(
        isinstance(accepted, list) and len(accepted) == 1,
        f"bulk sentinel did not return exactly one accepted row: {accepted!r}",
    )
    _ensure(rejected == [], f"bulk sentinel unexpectedly rejected rows: {rejected!r}")
    _ensure(
        accepted[0].get("geoid") == geoid,
        f"bulk sentinel geoid diverged: {accepted[0].get('geoid')!r} != {geoid!r}",
    )


def _validate_bulk_alias_parity(first: httpx.Response, second: httpx.Response) -> None:
    """The two bulk URLs are one operation — same status, same bytes, same media type."""
    _ensure(
        second.status_code == first.status_code,
        f"bulk alias status diverged: {second.status_code} != {first.status_code}",
    )
    _ensure(
        second.content == first.content,
        "bulk alias raw body diverged — decoded JSON equality is insufficient",
    )
    _ensure(
        second.headers.get("content-type") == first.headers.get("content-type"),
        (
            f"bulk alias content-type diverged: {second.headers.get('content-type')!r} "
            f"!= {first.headers.get('content-type')!r}"
        ),
    )


def run_mint_probe(client: httpx.Client) -> list[bool]:
    minted: dict = {}
    first_path, repeat_path, bulk_path, bulk_repeat_path = _sentinel_paths(
        COLLECTION, PUBLIC_COLLECTION
    )

    def _post_bulk_sentinel(path: str, label: str) -> httpx.Response:
        return record(
            label,
            client.post(
                f"{BASE}{path}",
                json={"type": "FeatureCollection", "features": [SENTINEL_FEATURE]},
                headers=_headers(),
            ),
        )

    def probe_mint() -> str:
        first = _post_sentinel(client, first_path, f"POST {first_path}")
        second = _post_sentinel(client, repeat_path, f"POST {repeat_path} (repeat)")
        minted.update(_validate_single_mints(first, second))
        return f"[201 ×2] byte-identical, geoid={minted['geoid']}"

    def probe_bulk() -> str:
        resp = _post_bulk_sentinel(bulk_path, f"POST {bulk_path}")
        _validate_bulk_mint(resp, minted["geoid"])
        if bulk_repeat_path == bulk_path:
            return f"[200] one accepted, no rejects, geoid={minted['geoid']}"
        repeat = _post_bulk_sentinel(bulk_repeat_path, f"POST {bulk_repeat_path} (repeat)")
        _validate_bulk_mint(repeat, minted["geoid"])
        _validate_bulk_alias_parity(resp, repeat)
        return f"[200 ×2] both bulk paths byte-identical, geoid={minted['geoid']}"

    def probe_resolve_geoid() -> str:
        # Resolver lives at the app root: BASE already carries the /geoid root_path.
        props = _get_json(client, f"/{minted['geoid']}").get("properties") or {}
        uri = props.get("uri")
        uri_netloc = urlsplit(uri or "").netloc
        _ensure(
            uri_netloc == _expected_netloc(),
            f"uri host {uri_netloc!r} != expected {_expected_netloc()!r} (uri={uri!r})",
        )
        return f"resolved; uri on {uri_netloc!r}"

    def probe_resolve_external() -> str:
        # The mint response names no collection, so this probes the collection
        # this run targeted. Dedup is global: if the sentinel geometry was first
        # minted into a DIFFERENT collection, no row exists here and this 404s —
        # re-run with GEOID_COLLECTION pointing at the incumbent's collection.
        collection = COLLECTION
        path = f"/collections/{collection}/external/{SENTINEL_EXTERNAL_ID}"
        if collection == PUBLIC_COLLECTION:
            resp = record(f"GET {path}", client.get(f"{BASE}{path}"))
            _ensure(resp.status_code == 400, f"expected 400, got {resp.status_code}")
            return "public collection: external_id lookup disabled → 400 (expected)"
        props = _get_json(client, path).get("properties") or {}
        _ensure(
            props.get("geoid") == minted["geoid"],
            f"external-id resolve returned {props.get('geoid')!r}, expected {minted['geoid']!r}",
        )
        return f"external_id {SENTINEL_EXTERNAL_ID!r} → same geoid"

    results = [_run_check("single-route aliases", probe_mint)]
    if not results[0]:
        print("  – resolve checks skipped (mint failed)")
        return results
    results.append(_run_check("bulk-route alias", probe_bulk))
    if not results[-1]:
        print("  – resolve checks skipped (bulk failed)")
        return results
    return results + [
        _run_check("resolve by geoid", probe_resolve_geoid),
        _run_check("resolve by external_id", probe_resolve_external),
    ]


# --- Runner -------------------------------------------------------------------


def _run_check(name: str, thunk: Callable[[], str]) -> bool:
    try:
        detail = thunk()
    except CheckFailed as exc:
        print(f"✗ {name:<24} {exc}")
        return False
    except httpx.HTTPError as exc:
        print(f"✗ {name:<24} {type(exc).__name__}: {exc}")
        return False
    except ValueError as exc:
        # A non-JSON body where JSON was expected (JSONDecodeError is a ValueError).
        print(f"✗ {name:<24} {type(exc).__name__}: {exc}")
        return False
    print(f"✓ {name:<24} {detail}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="GeoID post-deploy smoke test")
    parser.add_argument(
        "--mint",
        action="store_true",
        help="also mint the fixed sentinel feature (idempotent write probe)",
    )
    args = parser.parse_args()

    mode = "+ mint probe" if args.mint else "read-only"
    print(f"→ GeoID smoke test against {BASE}, collection {COLLECTION!r} ({mode})\n")
    with httpx.Client(timeout=30.0) as client:
        try:
            record("GET /conformance (reachability)", client.get(f"{BASE}/conformance"))
        except httpx.HTTPError as exc:
            print(f"✗ cannot reach GeoID at {BASE} ({exc})")
            return 1
        results = [_run_check(name, lambda fn=fn: fn(client)) for name, fn in READ_CHECKS]
        if BEARER_TOKEN:
            results += [
                _run_check(name, lambda fn=fn: fn(client)) for name, fn in ADMIN_READ_CHECKS
            ]
        else:
            print(
                "  – collection read checks skipped (no GEOID_BEARER_TOKEN; they are admin-gated)"
            )
        if args.mint:
            results += run_mint_probe(client)

    passed, total = sum(results), len(results)
    print(f"\n{'✓' if passed == total else '✗'} {passed}/{total} checks passed")
    print_timings()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

# ADR-009 — Idempotent mint, and a public surface narrowed to three operations

- **Status:** Accepted — 2026-07-25; authentication-invariance amendment accepted
  2026-07-27.
- **Reverses:** the 2026-07-09 dedup-409 disclosure rulings (round 1 and round 2) in full — the
  conflict they governed no longer exists. Also reverses the "a duplicate POST fails with 409
  carrying the incumbent geoid" rule set on 2026-06-11.
- **Implemented by:** `services/registry_service.create_place` / `_mint_one` (the collapsed dedup
  branch), `repositories/place_repo` (narrowed `InsertResult`), `api/places` (the `_writes` sub-router
  included twice — public and collection-scoped — plus `include_in_schema=False`),
  `main.create_app` (router-level hiding), `api.places._target_principal` (public-target
  identity suppression), and the authentication-free `api.places.resolve_geoid`.
- **Not changed:** managed-collection authorization, the identity recipe, the schema. No migration.

## Context

The client meeting fixed the **public** GeoID surface at three functionalities — post items, post
bulk, and a durable geoid resolver — and specified that a repeat upload of an identical geometry
must return the **already-minted geoid, with the same status code and body shape as a first mint,
carrying no signal that it was a duplicate**.

Four of the six rules the client stated already held in v0.9.0: geometry is pruned to geometry +
ingestion metadata (migration 0010), no authentication is needed for the public collection,
external_id does not differentiate in the public collection (migration 0012), and the identity is
derived from the geometry. Two did not:

| Client rule | v0.9.0 | Action |
|---|---|---|
| Only 3 endpoints public-facing | 16 operations, 12 visible in Swagger | hide 9 |
| Identical geometry → same geoid, no duplicate feedback | 409 `GeometryConflictError` + a caller-aware incumbent disclosure | rewrite |

So this is not a visibility change with a cosmetic tail: **the dedup contract inverts**.

## Decision

### 1. The mint is idempotent — all duplicates answer 201

`create_place` no longer raises on a dedup loser. The arbiter CTE already swallows a `geom_hash`
clash without aborting and derives the incumbent geoid in the same statement, so the service simply
falls through to the response it would have built for a winner. `GeometryConflictError`,
`GeometryConflictResponse` and the whole `_may_disclose_incumbent` disclosure machinery are deleted.
Bulk's `_mint_one` returns `BulkAccepted` where it used to return
`BulkRejected(reason="geometry_conflict")`, and that reason is gone from `BulkRejectReason`.

One guard survives and must not be simplified away: if the repo cannot resolve an incumbent
collection for a loser (`collection_slug is None` — genuine registry/recipe drift), the service
still raises `RegistryConsistencyError` → a structured 500. Returning a 201 whose geoid does not
resolve would be strictly worse than an error. `place_repo._resolve_incumbent` keeps its bounded
retry for the same reason: without it, the concurrent pre-commit window would produce spurious 500s
from that guard.

### 2. `MintResponse` drops `collection`

The response reports the geoid, not where the row lives. This is what dissolves the probe oracle:
with no slug in the body, a repeat POST is byte-shape-identical to a first mint and reveals nothing
about private collections — which is precisely what the deleted disclosure gate existed to protect.
`uri` stays (it *is* the geoid in resolvable form, and the client's third requirement) and
`external_id` stays (it echoes what the caller submitted, so collection-scoped internal callers can
correlate).

`collection.public_read` becomes functionally inert — the 409 disclosure was its last consumer. The
column drop is deferred to the hardening bundle alongside `api_key`; the ORM mapping stays for
schema alignment.

### 3. Three public operations; everything else is hidden, not removed

`POST /items` and `POST /items/bulk` are the public paths of the *same two operations* the
collection-scoped URLs serve. Each write operation is declared **once**, on a `_writes` sub-router
that `places` includes twice: bare for the public paths, and with
`prefix="/collections/{collection_id}", include_in_schema=False` for the scoped ones
([FastAPI documents including one router under several prefixes][include-twice]). A dependency
resolves the target collection from `request.path_params` when the scoped path supplied it and from
`settings.public_collection` otherwise; it is deliberately **not** a signature parameter, since on
`/items` FastAPI would infer it as a required query parameter and let a caller aim the public path
at any collection.

One handler therefore backs both URL shapes, so response model, status code, dependency graph,
validation contract and response metadata cannot drift apart — `endpoint is endpoint` identity is
pinned in `tests/unit/test_route_structure.py`, and `tests/integration/test_public_items.py` pins
that the pairs agree on failures (422, 413) as well as successes. The earlier design — separate
public handlers that *called* the scoped ones — kept the business logic single-sourced but left the
HTTP contract declared twice; a scoped handler gaining a `Depends(...)`-defaulted parameter would
have been called with the unresolved `Depends` object rather than failing.

Both includes precede the `/{geoid}` catch-all, since `items` is a literal single segment.

[include-twice]: https://fastapi.tiangolo.com/tutorial/bigger-applications/#include-the-same-router-multiple-times-with-different-prefix

Hiding uses static `include_in_schema=False` — router-level for `health`, `ogc`, `grants` and
`manage`, at the scoped include for the two `places` write operations, per-route for `/me/geoids`.
Not a second router object for `places` as a whole, because its registration order is a load-bearing
invariant (the root `/{geoid}` catch-all must come last), pinned by
`tests/unit/test_route_structure.py`. Consequence of the shared handlers: were the scoped routes
ever unhidden, their `{collection_id}` would appear in the schema undocumented — it is absent from
the signatures by design.

**Hiding is cosmetic, never an authorization control.** Every hidden route stays live and keeps its
existing gate. That this is true is proved on every deploy: `scripts/smoke_test.py` exercises `/`,
`/conformance`, `/collections`, `/collections/{id}` and the external-id resolver, and `deploy.yml`
hard-gates on `GET /health` — all of them absent from the schema.

### 4. The three public operations are authentication-invariant

Until the public authentication contract is revisited, the three published operations behave as
anonymous operations even when an `Authorization` header is present. A valid, malformed, expired,
or non-Bearer credential cannot change their status, body, headers, cacheability, or side effects,
and cannot turn a request into a 401.

For both write URL shapes, `_target_principal` first resolves the target collection. The configured
public collection always receives `Principal.anonymous()` **before credential extraction**, so
public mints persist `provenance.created_by=null`. This applies equally to `/items` and the hidden
`/collections/{public}/items` alias and to their bulk variants. A managed collection still extracts
and validates Bearer credentials through the same `principal_from_credentials` seam used by
`require_principal`, preserving its existing write ladder and 401/403 contract.

`GET /{geoid}` declares no principal dependency and always builds the anonymous masked feature:
geometry plus `{geoid, uri}`. Its cache policy is likewise caller-independent (`public` with an
ETag when enabled, and `Vary: Accept`, not `Authorization`). The hidden external-id resolver remains
caller-aware: members can receive the full feature, authenticated responses remain private, and bad
Bearer credentials still 401 there.

This explicitly supersedes the 2026-07-09 caller-aware rule for `GET /{geoid}` and the later rule
that a malformed credential must 401 on that route. It does not remove OIDC or grants from hidden
managed surfaces.

## Consequences

- **Public callers have one representation and one identity posture.** Signing in cannot reveal
  metadata on `GET /{geoid}`, associate a public contribution with `/me/geoids`, or reduce shared
  cacheability. Clients may send stale or irrelevant credentials without changing the operation.
- **This is a client-visible resolver change.** Authenticated creators, grant holders, and
  sysadmins now receive the same masked geoid response as anonymous callers. Client comms are owed
  before deployment.
- **Breaking for anyone already integrated.** A 409 becomes a 201 and a response field disappears.
  A client that branched on 409 to detect "already registered" now cannot distinguish the cases —
  which is the point, but it needs saying out loud. Client comms are owed.
- **A batch can now report the same geoid more than once.** In-batch geometry twins are all
  `accepted`, each carrying the one geoid that geometry mints. `BulkReport` documents this.
- **Re-submission is now the recommended recovery.** Re-running a bulk import, a seed, or the
  deploy smoke probe converges instead of accumulating rejects. `scripts/smoke_test.py --mint`
  submits its sentinel twice and asserts the two 201s are byte-identical — the deploy gate on this
  ADR's central rule.
- **The 2026-07-09 disclosure work is retired, not paused.** Reviving it would require reinstating
  a conflict for it to attach to. The masking rationale it rested on is now satisfied structurally
  (the body carries nothing to mask) rather than by a runtime gate — a strictly stronger position.
- **The geometry-uniqueness invariant is untouched.** `UNIQUE (geom_hash)` on `geoid_registry`
  still enforces one geometry → one geoid globally; only the HTTP expression of a collision changed.
- **`uq_geoid_registry_geom_hash` should now never reach an error handler.** The `IntegrityError`
  backstop in `api/errors.py` is retained and still logs loudly if it ever does.

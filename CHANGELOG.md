# Changelog

All notable changes to GeoID are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.2] - 2026-07-27

### Changed

- The deployed synchronous bulk cap is now **4,000 in review and production**,
  certified on the smaller 1 vCPU / 1 GiB review service for simple farm-plot
  payloads at up to two concurrent requests. The application default remains
  1,000, and the deployed-scale integration timing gate exercises 4,000
  features.
- Capacity evidence now includes exact wire sizes, bounded 2 KiB-property and
  100-vertex geometry probes, Cloud Run/Cloud SQL resource peaks, direct versus
  load-balanced request timing, and verified campaign cleanup.

## [0.10.1] - 2026-07-27

### Fixed
- GeoJSON resolver self links now carry the title `GeoJSON` instead of a null
  title, matching the existing `WKT` title on alternate links.

## [0.10.0] - 2026-07-27

### Changed
- **The three public operations are authentication-invariant until further
  notice.** `POST /items`, `POST /items/bulk`, and `GET /{geoid}` ignore valid,
  malformed, expired, and non-Bearer credentials. Public mints record
  `created_by: null`; the resolver always returns the same geometry +
  `{geoid, uri}` body and public-cache policy. Managed collection writes and the
  hidden external-id resolver keep their existing authentication and
  authorization behavior.
- **Registering a geometry is now idempotent.** Submitting a shape that is
  already registered returns **201 with its existing geoid** — the same status
  code and the same body as a first registration, with no indication that it was
  a duplicate and no second record written. Re-uploading a file, retrying after a
  timeout, or sending overlapping batches now simply converge; there is nothing
  to check first and nothing to reconcile afterwards. **This is a breaking change
  for anyone who branched on the previous 409.**
- **The mint response no longer carries a `collection` field.** It reports the
  geoid and its resolvable URI — not where the record was filed. As a result a
  repeat submission is byte-for-byte identical to a first one and discloses
  nothing about collections the caller cannot see.
- In bulk, an already-registered geometry is reported under **`accepted`** with
  the geoid it already has, rather than as a rejection. A batch containing two
  copies of the same shape therefore accepts both, carrying the same geoid.

### Added
- **`POST /items`** — register one place. The public entry point; no
  authentication required.
- **`POST /items/bulk`** — register many places in one request, same terms.

### Removed
- The duplicate-geometry **409** and its response model, together with the
  caller-aware incumbent-disclosure rules introduced on 2026-07-09 — with no
  conflict to report and no collection in the response, they no longer have
  anything to govern.
- The `geometry_conflict` per-feature reject reason from bulk reports.
- **Nine operations from the published API documentation.** The public surface
  is now exactly three: register a place, register many, and resolve a geoid.
  Collections, `external_id`, grants, `/manage`, `/me/geoids` and the OGC read
  surface remain **fully live with unchanged access rules** — they are simply no
  longer advertised while decisions about their access model are finalised.
  Hiding them is a documentation change, never a security control.

## [0.9.0] - 2026-07-17

### Added
- **Executable request-body examples in Swagger** for the single and bulk mint
  endpoints: obviously-dummy geometries at the identity precision (7 decimals),
  distinct between the two endpoints so first tries never cross-conflict, with
  the `collection_id` path field prefilled with `public`. Repeat clicks answer
  the documented dedup 409 — a live demonstration, not an error.
- A **Performance** section in the README with measured staging numbers: bulk
  ingest ~190 features/s, reads linear to ~280 RPS, ~91 single mints/s, and the
  scaling characteristics behind them.

### Changed
- **The `properties` member is now optional on write bodies** (deliberate
  RFC 7946 §3.2 leniency): a Feature may omit it or send `null` instead of an
  empty object. Submitted properties remain accepted-but-never-persisted, per
  0.6.0.
- The post-deploy smoke test is now a **hard release gate** run against the
  direct service URL on every deploy (review deploys also exercise the full
  write path via an idempotent sentinel); minted links are still verified
  against the public base URL.

### Removed
- The stale k6/locust load-test scripts, which predated the dedup-409 contract;
  `scripts/loadtest.py` is the maintained load harness.

## [0.8.0] - 2026-07-16

### Removed
- **Supersession (`predecessor`) support is removed entirely** — the concept
  has no basis in OGC API Features (Part 1 or the Part 4 draft). The
  always-null `predecessor_geoid` feature property and the never-rendered
  `predecessor-version` link are gone from feature responses, and the unused
  `place.predecessor_id` column (never writable, never populated) is dropped
  by migration 0013. Corrections continue to mint a new geoid.

### Changed
- The immutability error message now reads "place is immutable; corrections
  mint a new geoid" (it no longer names the removed `predecessor_id` column).

## [0.7.0] - 2026-07-16

### Changed — BREAKING
- **The public default collection no longer resolves features by
  `external_id`.** `GET /collections/public/external/{external_id}` now
  answers 400 with an explicit message ("external_id lookup is not available
  in the public collection") — deliberately not a 404, which stays reserved
  for genuinely unknown ids. Resolution by geoid (`GET /{geoid}`) is
  unaffected. Private/managed collections keep the external-id resolver
  unchanged.
- **Duplicate `external_id` values are now accepted in the public default
  collection.** Submitted values are still stored and echoed on member reads,
  but they are no longer unique there — re-submitting an `external_id` with a
  different geometry mints normally instead of failing with 409. Private/
  managed collections keep exact per-collection uniqueness and the 409
  conflict answer.

### Changed
- The configured public-collection slug (`GEOID_PUBLIC_COLLECTION`) is now
  validated at startup; a malformed value fails boot loudly instead of
  seeding an unreachable collection.

## [0.6.0] - 2026-07-16

### Changed — BREAKING
- **Submitted feature properties are no longer stored or returned.** A GeoJSON
  Feature's `properties` object (and any unknown top-level member) is still
  accepted on every write route, per RFC 7946 — but the registry now persists
  only the geometry and its own ingestion record. Full feature bodies (member
  read) no longer echo submitted attributes: `properties` carries exactly the
  server-derived fields (`geoid`, `uri`, `external_id`, `created_at`,
  `originating_instance`, `_geoid_provenance`, and `predecessor_geoid` when
  set). The `_geoid_provenance` block is now exactly
  `{schema: "geoid-prov/0.2", created_by, originating_instance}` — the
  `client` descriptor and `submitted_properties` are gone. Properties already
  stored by earlier versions are removed by migration; minted geoids, dedup
  behavior, and all identifiers are unaffected.

### Fixed
- Requests carrying NUL bytes (a `%00` path parameter or an escaped `\u0000`
  in a JSON body) now answer 422 with a clear message instead of 500. A NUL
  inside a bulk FeatureCollection aborts the whole batch as 422 with nothing
  persisted; resubmitting the cleaned batch converges via dedup.

### Added
- **Optional HTTP caching for the public resolvers**, off by default
  (`GEOID_RESOLVER_CACHE_MAX_AGE=0`). When enabled, anonymous 200s from
  `GET /{geoid}` and the external-id lookup carry a strong `ETag`,
  `Cache-Control: public, max-age=<n>` and `Vary`, and answer conditional
  `If-None-Match` requests with 304; authenticated responses are always
  `private, no-store` and never cached. Disabled in production.

## [0.5.2] - 2026-07-11

### Changed
- **Production now keeps one warm instance** (`min-instances 1`, 4 GiB memory),
  so requests arriving after an idle period skip the multi-second cold start.
  The review environment is unchanged (scale-to-zero, 1 GiB).

### Added
- **JWKS signing keys are prefetched at startup**, so the first authenticated
  request after a cold start no longer pays the 200–340 ms key fetch.

## [0.5.1] - 2026-07-10

### Fixed
- **Single sign-on no longer re-prompts users who already have a live session.**
  The API docs' "Single Sign-On" login now requests the standard
  `openid profile email` scopes on its authorization redirect, so an existing
  realm session is honored and the browser returns already authorized instead of
  showing the login page again. The scopes are a fixed default — the Authorize
  dialog presents no scope checkboxes.

### Added
- `CONTRIBUTING.md` — how to set up a development environment, run the test
  suite, and submit changes.

### Removed
- The provisional data-license placeholder; the repository is licensed
  Apache-2.0 only.

## [0.5.0] - 2026-07-09

### Changed — BREAKING
- **Full feature bodies are now member-only on both resolvers.** `GET /{geoid}`
  and `GET /collections/{id}/external/{external_id}` return the complete feature
  (attributes, provenance, external id, timestamps, collection link) only to
  sysadmin, the feature's original submitter, or holders of any grant (`viewer`
  and up) on its collection. Every other caller — anonymous included — receives a
  masked, geometry-only body: properties reduced to `geoid` and `uri`, links
  reduced to self and the WKT alternate. This is independent of `public_read`.
- **External-id lookup no longer hides existence — it now behaves exactly like
  geoid lookup.** An existing `(collection, external_id)` answers 200 to every
  caller (full or masked body per the rule above); 404 now always means the id
  genuinely does not exist. This supersedes v0.4.0's behavior, where a
  `public_read: false` collection answered the same 404 as an unknown id.
  `public_read`'s single remaining function is naming the incumbent in the
  duplicate-geometry 409 — that disclosure rule itself is unchanged: the 409
  carries the incumbent geoid/uri/collection when the incumbent's collection is
  publicly readable or the caller is a member of it, and null fields otherwise.
- **`writable_anon` is renamed `public_write`** in the collection management API
  (create request and responses). The flag's meaning is unchanged: anyone,
  anonymous included, may mint into the collection.
- `GET /{geoid}` now validates a presented bearer token: a malformed or invalid
  `Authorization: Bearer` header answers 401 instead of being silently ignored.
  Requests with no credentials at all are still anonymous and still resolve.

### Changed
- Granting the `owner` role now requires sysadmin (403 otherwise); a collection
  owner grants `editor`/`viewer` only. Demoting or revoking another owner remains
  an owner-level operation, still subject to the last-owner guard.

### Fixed
- Re-granting an existing grantee with a different role now returns the updated
  role in the response (a stale cached row was previously echoed).

## [0.4.0] - 2026-07-08

### Changed — BREAKING
- **Authentication is Keycloak-only: the static admin token is removed.** Every
  authenticated call now presents a Keycloak access token (RS256 JWT); the
  `GEOID_ADMIN_TOKEN` bearer no longer exists, and the global sysadmin tier is
  granted solely by the `geoid.sysadmin` role. Outside development the service
  refuses to start unless OIDC is configured, so a deployed environment can
  never run with zero authentication paths. Anonymous access is unchanged
  (public reads, and writes into the anonymous-writable collection).
- **Identity recipe v2 (migration 0008, ADR-007): every geoid re-mints.** The
  geometry fingerprint the geoid is derived from is no longer computed by the
  database's GEOS library (whose builds provably disagree with each other); it is
  an engine-independent integer-lattice canonicalization owned by GeoID — the
  same geometry now yields the same geoid on *any* engine, build, or deployment.
  Geoids minted under the previous recipe are void (the registries were reset per
  the re-mint runbook). Dedup semantics are unchanged (same ~1cm exact-match
  precision, same 409-with-incumbent contract).
- **New:** a geometry that *degenerates at the identity precision* — e.g. a
  polygon sliver thinner than ~1cm, or a zero-area bowtie — is now rejected with
  `422 "geometry degenerates at the identity precision (1e-7°)"` (one
  `schema_invalid` reject on the bulk route) instead of being silently merged or
  dropped by the old repair step.
- The golden-vector corpus is regenerated as v2 and is now verifiable without a
  database; the post-deploy canary asserts the deployed SQL recipe is
  byte-identical to the pure-Python reference.

### Added
- **Keycloak authentication** — access tokens (RS256 JWT) validated against the
  FAO realm, mapping the `geoid.sysadmin` role to the global admin tier. Enabled
  at both deployed environments (review and production realms).
- **Per-collection RBAC grants** — `owner`/`editor`/`viewer` roles keyed by
  verified email, managed via
  `POST`/`GET`/`DELETE /collections/{id}/grants[/{email}]` (owner or sysadmin).
- **Private collections** — a collection can be marked `public_read: false`
  (migration 0009): its features are hidden from the external-id resolver (the
  same 404 an unknown id gets) except to sysadmin or grant holders (`viewer` and
  up), and the duplicate-geometry 409 names the incumbent geoid only to callers
  allowed to read the incumbent's collection. Resolution by geoid
  (`GET /{geoid}`) is deliberately unaffected: the geoid itself is the read
  capability.
- `GET /me/geoids` — the authenticated caller's own minted geoids, newest first.
- Sysadmin item inventory: `GET /manage/collections/{id}/items` lists a
  collection's records (geometry-free) for administrators.
- Swagger single-sign-on button (Authorization Code + PKCE), on at the deployed
  environments (gated behind `GEOID_SWAGGER_OAUTH2_ENABLED` elsewhere).
- Post-deploy golden-vector canary: `dedup_vectors.py --check` runs as a Cloud Run
  job against the live database after every migration, failing the deploy on
  identity-recipe drift before the service ships.
- Last-owner guard: revoking or demoting a collection's only `owner` grant is
  blocked with 409 (grant another owner first).
- Database hardening (migration 0007): normalized-grant-email CHECK constraint;
  identity/dedup SQL functions marked `PARALLEL SAFE` with a pinned `search_path`
  (bodies unchanged — identity untouched); dead paging index dropped. (2D-only
  needs no new constraint: the geometry column type already rejects Z/M.)

### Changed
- A grant whose recorded Keycloak subject differs from the caller's is no longer
  honored (403 + server-side warning) — email reuse cannot silently inherit access.
- Unrecognized database integrity errors now answer `500 internal error` (logged
  loudly server-side) instead of a mislabeled `409`.
- Bulk ingest documents its atomicity contract: the batch is one transaction — a
  timeout/disconnect before the response discards all rows, including accepted ones.

### Fixed
- Non-ASCII bearer tokens now answer 401 instead of crashing with a 500.
- The dedup 409 now always names the *stored* incumbent geoid (self-correcting
  against any legacy pre-deterministic row).
- Database/programming failures during a write are no longer masked as
  `422 unparseable GeoJSON`; they surface loudly with server-side logging.
- Application logs (OIDC rejections, JWKS outages, bootstrap) are actually emitted
  in the deployed image; JWKS infrastructure failures log at ERROR.
- Concurrent cold-start bootstrap no longer crashes on the public-collection
  create race.
- Two identical geometries POSTed at the same instant now always converge on the
  ordinary 409-with-incumbent; the loser of the internal insert race no longer
  surfaces an internal error.

## [0.3.0] - 2026-06-24

### Added
- Accept Point and MultiPoint geometries (in addition to Polygon/MultiPolygon);
  lines and GeometryCollection remain unsupported. Identity reuses the frozen v1
  recipe at the same 1e-7 tolerance, so a point dedups exactly as a polygon vertex
  does (409 + incumbent on a match). See ADR-005.

### Changed
- GeoID is now explicitly 2D-only: a geometry carrying a Z (elevation) coordinate
  is rejected with a 422 instead of being silently accepted. See ADR-006.

## [0.2.1] - 2026-06-23

### Changed
- Scrubbed internal planning and team references from API descriptions, code
  comments, and OpenAPI docstrings. Documentation only — no behavioral change.

## [0.2.0] - 2026-06-23

### Removed
- **BREAKING — item listing/filtering/search endpoints removed.** Querying
  items by attribute or bbox is no longer supported via the API.
- **BREAKING — collection reads now require an admin principal.** Listing and
  reading collections is gated behind admin.
- OGC endpoints are hidden from the OpenAPI/Swagger schema.

## [0.1.2] - 2026-06-23

### Changed
- Bulk ingest cap raised to 2,500 features per request (deployed with 1 GiB
  memory and a 600 s request timeout).

### Fixed
- `/health` and OpenAPI now report the real package version (derived from
  package metadata) instead of a hardcoded string.

## [0.1.1] - 2026-06-23

### Security
- Patched `pydantic-settings` to 2.14.2 (GHSA-4xgf-cpjx-pc3j).

## [0.1.0] - 2026-06-22

Initial production release.

### Added
- **Deterministic, content-addressed GeoID** — each ID is a UUIDv8 derived from
  the geometry itself.
- **Global geometry uniqueness** — ingesting a duplicate geometry returns `409`
  with the incumbent geoid.
- **Synchronous multi-geometry bulk POST** for batch ingest.
- **GeoJSON + WKT** geometry output via `?format=`.
- **Durable resolver** served at the app root (`/{geoid}`).
- Collection management API.
- `/health` database-connectivity probe.
- Versioned dedup recipe with pinned golden vectors and a documented re-hash
  procedure.
- Proxy sub-path mount support (`GEOID_ROOT_PATH`).

### Security
- OGC callback hardened against SSRF to internal hosts.
- Patched `starlette` / `cryptography` / `aiohttp`.

# ADR-004 — Deterministic, content-addressed geoid (UUIDv8 from the geometry hash)

- **Status:** Accepted — 2026-06-18. Superseded in part by
  [ADR-007](ADR-007-identity-recipe-v2.md) (recipe v2, 2026-07-02): the UUIDv8 derivation and the
  freeze *discipline* stand unchanged; the frozen v1 recipe body this ADR describes was replaced by
  the engine-independent integer-lattice recipe, executed as exactly the identity-version event this
  ADR defined. *(File deleted from the tree on 2026-06-22 and restored verbatim on 2026-07-02 — it
  was only ever missing from the working tree, never from git history.)*
- **Supersedes:** the random-UUIDv7 geoid (RFC 9562 §6.2 monotonic minting)
- **Implemented by:** migration `0004_deterministic_geoid`, `domain/identifiers.geoid_from_geom_hash`,
  `repositories/place_repo.insert_place`

## Context

Until now a geoid was a **random UUIDv7** minted app-side, independent of the geometry. It is
time-ordered but carries fresh OS randomness, so it is **not deterministic**: the same geometry
submitted to a second deployment — or re-created after a wipe — receives a *different* geoid.
Determinism lived only in the separate `geom_hash` (SHA-256 of the canonicalized geometry) used for
global dedup.

GeoID is a *"global, federated registry."* Federation under random geoids requires a future central
authority to reconcile per-instance identifiers. The stakeholders asked whether the geoid can instead
be **deterministic**, so the same geometry resolves to the same geoid everywhere with no coordination
(and re-import / delete-then-recreate becomes idempotent).

## Decision

Make the geoid a **deterministic, content-addressed UUIDv8** (RFC 9562 §5.8): the first 16 bytes of
the geometry's canonical SHA-256 `geom_hash`, with the version (8) and RFC 4122 variant bits stamped.
It is computed **DB-side** in the arbiter CTE via `geoid_id_default(geom)` =
`geoid_from_geom_hash(geoid_geom_hash_default(geom))`, from the **same** hash used for dedup, so
identity and dedup are one fingerprint and cannot drift. `domain/identifiers.geoid_from_geom_hash` is
a byte-identical Python mirror for tests/tooling; the DB value is authoritative.

### Why UUIDv8, not UUIDv5 (the version we initially preferred)

UUIDv5 (name-based, SHA-1) is the more universally recognized "deterministic UUID," and v8 is labeled
"experimental." But:

1. **RFC 9562 §6.5 mandates v8 for SHA-256:** name-based UUIDs from SHA-256 or newer *"MUST NOT
   utilize UUIDv5 and MUST be within the UUIDv8 space."* v5 is hard-wired to SHA-1.
2. We **already** compute a SHA-256 `geom_hash`. v8 makes the geoid a literal re-encoding of those
   bytes — one recipe, no new crypto call, no namespace. v5 would force a second, weaker SHA-1 path.
3. **"Experimental" describes v8's purpose** (a custom-scheme sandbox), not its maturity (ratified,
   RFC 9562, May 2024). Our consumers (Postgres `uuid`, asyncpg, OGC clients, federation partners) all
   treat the geoid as an **opaque UUID string** and never branch on the version nibble; the one real v8
   gap — some libraries lack a v8 *generator* — does not apply since we generate DB-side.

### Rejected alternatives

- **UUIDv5 / SHA-1** — weaker hash, second hashing path, RFC-disfavored for SHA-256. (See above.)
- **Raw SHA-256 string** — breaks `place.id`'s `uuid` type, every FK, and every OGC item URL.
- **ULID / KSUID / Snowflake** — time/random, not deterministic.
- **Geohash / S2 / H3** — identify a *cell/location*, not a specific polygon (different polygons collide).

## Consequences

- **(+) Federation by construction:** same geometry → same geoid on every deployment, zero
  coordination; delete/re-create and re-import are idempotent; stable cross-environment permalinks.
- **(−) The canonicalization recipe is now IDENTITY-load-bearing and therefore FROZEN.** The grid
  (`1e-7`) and the PostGIS/GEOS output bytes now determine identity, not just dedup. A recipe or GEOS
  change that shifts a canonical vertex would **re-mint geoids and break permalinks** — so it is an
  *identity-version* event, never a survivable `rehash_geom_hashes.py` re-hash. The golden-vector oracle
  and `dedup_recipe_stamp` now guard identity, not just dedup correctness.
- **(−) Lost PK k-sortability:** deterministic ids distribute randomly in the primary-key b-tree (more
  page splits / insert I/O). Negligible at the ~40k-polygon Release-1 scale; matters on the billion-row
  `local-scripts/docs/SCALING.md` ladder. Read paging is unaffected (it orders by `place(collection_id, created_at, id)`).
- **(=) Collision space:** 122-bit payload (16-byte truncation of SHA-256) → birthday ~2⁶¹; negligible.

## Rollout precondition

Existing random-v7 geoids are immutable permalinks and cannot be re-minted, so this **must** land
before any durable data exists. It shipped as a **clean cutover** (review DB reset → migrate → re-seed);
there was no production data. A mixed v7/v8 population is explicitly avoided.

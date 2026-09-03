# ADR-007 — Identity recipe v2: engine-independent integer-lattice canonicalization

- **Status:** Accepted — 2026-07-02 (senior-review finding H5; sanctioned identity-version event)
- **Supersedes:** [ADR-004](ADR-004-deterministic-geoid.md) (deterministic geoid; *recipe frozen
  for all time*) **in part** — the UUIDv8 derivation and the freeze *discipline* stand; the frozen
  v1 *recipe body* is replaced, executed as the identity-version event ADR-004 itself defined
- **Extends:** [ADR-005](ADR-005-point-geometries.md) (point/multipoint),
  [ADR-006](ADR-006-2d-only.md) (2D-only; the degeneracy REJECT below reuses its rationale)
- **Implemented by:** migration `0008_identity_recipe_v2` (SQL) + `domain/geometry_identity.py`
  (byte-identical pure-Python reference) + `scripts/data/dedup_golden_vectors_v2.json` (corpus)

## Context

Since migration 0004 the geoid **is** the geometry fingerprint (UUIDv8 over the first 16 bytes of
`geom_hash`), so the canonicalization recipe is identity-load-bearing. Recipe v1 delegated
canonicalization to GEOS:

```
geom_hash = sha256(ST_AsBinary(ST_Normalize(ST_ReducePrecision(ST_MakeValid(geom), 1e-7)), 'NDR'))
```

`ST_ReducePrecision`, `ST_Normalize`, and `ST_MakeValid` are all GEOS-bound, so **identity depended
on the deployed GEOS build**. This is not theoretical — the drift is demonstrated at commit level:

- GEOS 3.9/3.10 `PrecisionModel::makePrecise`: `round(val * scale)` with `scale = 1/gridSize`.
- GEOS 3.11 (commit `20a807439fe6`, port of JTS PR #804): `round(val / gridSize) * gridSize`.
- GEOS 3.13 (commit `a423241d8afd`): the guard becomes `gridSize > 1`, so sub-unit grids **revert**
  to the 3.9 arithmetic. Three incompatible behaviors in four minor versions.
- Two concrete mechanisms (414/1220 probed coordinates drifted between builds): (i) tie-direction
  flips (`val*1e7` vs `val/1e-7` land on opposite sides of .5); (ii) **reverse-scale bit
  mismatch** — the same lattice cell reconstructs to a *different double bit-pattern*, so the WKB
  bytes (and the hash) differ even when the cell agrees.
- `ST_ReducePrecision` runs GEOS in `GEOS_PREC_VALID_OUTPUT` mode: snap-rounding can **add and
  move** vertices, and **collapsed components are silently removed** — a sliver that collapses at
  1e-7 vanishes, so *all* such slivers hash identically (a silent identity merge, discovered during
  this work).

Locally this showed up as the strict/advisory golden-vector split: CI (GEOS 3.9.0) could not
reproduce the Cloud SQL (GEOS 3.11.4) digests for `cell_straddle_high`/`grid9e5_*`. For a
federation-grade identifier ("same geometry → same geoid on every deployment") that is untenable:
a managed Cloud SQL GEOS upgrade would re-mint every geoid.

A cross-library survey (JTS, Shapely 2 `set_precision`, turf, PostGIS `ST_SnapToGrid`, DuckDB
spatial, GDAL, tg, S2, geohash/H3/Placekey/UBID, Overture GERS) confirmed the pattern: systems that
*repair inside the snap* (JTS/GEOS/Shapely/DuckDB defaults) are exactly the version-fragile camp;
reproducible systems (S2 E7, geohash, H3, UBID) **bin coordinates into an integer lattice first and
never hash raw float bytes** — the lattice *is* the tolerance. Nobody sane hashes reconstructed
floats. S2's E7 (`round(1e7 × degrees)` into an int) is the direct precedent for v2 — except S2
explicitly disclaims its tie direction, which v2 pins.

## Decision

Replace the GEOS canonicalization with an **integer-lattice canonicalization we fully own**, in
both SQL (migration 0008, `PARALLEL SAFE`, pinned `search_path`, GD001 degeneracy errors) and pure
Python (`domain/geometry_identity.py`), pinned byte-identical by a full-corpus parity suite. The
geoid derivation is unchanged in shape: `geoid = UUIDv8(first 16 bytes of geom_hash)` via the
untouched `geoid_from_geom_hash`/`geoid_id_default` (0004). Only the hash body changes. Zero
GEOS/PostGIS calls remain in the identity bytes — only coordinate *accessors* (`ST_DumpPoints`,
`ST_X`/`ST_Y`, ring/part accessors, `GeometryType`), all liblwgeom-native.

**The frozen v2 spec** (a future change is another identity-version event):

1. **Quantization.** `q = round_half_even(coord × 10000000.0)` — ONE IEEE-754 double multiply by
   the integer `SCALE = 10^7` (exactly representable as a float64; `1e-7` is **not** — multiply,
   never divide). `|lon|·1e7 ≤ 1,800,000,000` fits int64 with ~5× headroom. **Only int64 lattice
   indices are ever hashed, never reconstructed floats** — killing drift mechanism (ii) and IEEE
   `-0.0` by construction.
2. **Rounding rule: half-even (rint semantics) on the exact binary product.**
   SQL: `(c * 10000000.0::float8)::bigint` — PG's float8→bigint cast applies C `rint()` =
   ties-to-even (the `::float8` is load-bearing; a `numeric` path rounds ties away-from-zero).
   Python: `round(coord * 10000000.0)` — identical. Rejected: half-up/`floor(x+0.5)` (GEOS/JTS
   style; carries the classic double-rounding bug at `x = 0.49999999999999994`) and half-away
   (C `round()`; not natively available in either runtime). Pinned by exact-tie golden vectors
   (`5e-8×1e7` is exactly `0.5` → 0; `-5e-8` → 0; `1.5e-7` → 2; `2.5e-7` → 2).
3. **Canonical structure** (v1 delegated this to `GEOSNormalize`; v2 owns it as spec constants):
   - **Ring:** quantize every listed vertex → remove consecutive duplicate lattice points
     **cyclically** (forward pass + wrap-around trim — subsumes the closing vertex, so no step
     depends on parser closure representation) → if fewer than 3 vertices remain OR the exact
     integer shoelace is 0 → **degenerate, REJECT** → orient every ring **CCW** (ring role is
     positional, so orientation need not encode it) → rotate the lexicographically smallest
     `(x, y)` vertex to front (a repeated minimum — a lattice pinch — tie-breaks to the rotation
     with the smallest vertex sequence, element-wise integer compare in both implementations).
     The shoelace is exact arithmetic (PG `numeric`, Python `int`): 2× the world's lattice area
     exceeds int64. **Collinear vertices are never removed** — `collinear_extra_vertex` keeps its
     own identity (v1 rule preserved); only coincident (zero-length) runs collapse.
   - **Polygon:** exterior ring first, then holes sorted by canonical serialized bytes.
   - **MultiPolygon:** polygon bodies sorted by canonical bytes. Byte sort = bytea memcmp =
     Python `bytes` ordering; no collation anywhere.
   - **MultiPoint:** members sorted by `(x, y)`, **duplicates kept** (no silent merge).
4. **Serialization** (big-endian throughout; deliberately our own format, not WKB):

   ```
   canonical_bytes = 0x02 (version) || type_tag || body
   type_tag: Point=0x01  MultiPoint=0x02  Polygon=0x03  MultiPolygon=0x04
   ring            = int4 count || count × (int8 qx || int8 qy)
   Polygon body    = int4 nrings || exterior || sorted holes
   MultiPolygon    = int4 nparts || sorted polygon bodies
   MultiPoint body = int4 n || sorted (qx, qy) pairs        Point body = qx || qy
   geom_hash_v2    = sha256(canonical_bytes)
   ```

   PG `int4send`/`int8send` (big-endian two's-complement) ≡ Python `struct.pack('>i'/'>q')`.
5. **No repair of any kind in the hash pipeline** — `ST_MakeValid` is dropped. Validity is a
   boundary concern (schema 422 + `ck_place_geom_is_valid`), so a stored geometry is always valid;
   v1's MakeValid ran *before* ReducePrecision and never repaired snap-induced degeneracy anyway
   (VALID_OUTPUT did that, silently and version-sensitively); and the noder/repair path is the most
   version-sensitive part of GEOS. The one real edge — a valid geometry that degenerates *on the
   lattice* — is **rejected at mint (422 "geometry degenerates at the identity precision
   (1e-7°)")**, mirroring ADR-006: an identity service must not silently merge distinct
   submissions, and (unlike v1) must not silently *vanish* them either.
6. **Degeneracy surfacing.** SQL raises custom SQLSTATE **`GD001`** — deliberately not 23514,
   whose handler asks `ST_IsValidReason` (it answers "Valid Geometry" for a lattice-degenerate
   sliver). Enforcement is two-layer: a schema pre-check in `PlaceCreate` (clean early 422,
   uniform for GeoJSON + WKT, single + bulk → `schema_invalid`) and the DB backstop
   (`registry_service` maps GD001 → `GeometryInvalidError` → 422; bulk twin → one
   `invalid_geometry` reject). Empty/unsupported-type inputs keep their legacy 23514 class.
7. **No grid parameter.** v1's `geoid_geom_hash(g, grid)` generality is dropped and the function
   itself is **dropped** by 0008 (no live GEOS-bound hash stays callable on an identity database).
   `GEOID_DEDUP_GRID_DEFAULT` stays `1e-7` as the documented cell size; a unit test pins
   `geometry_identity.SCALE == migration-0008 literal == round(1 / dedup_grid_default)`. Retunes
   remain migration events — now identity-version events.

## Lessons table (research gates 1a/1b — each footgun with v2's answer)

| # | Footgun (evidence) | v2 answer |
|---|---|---|
| 1 | Tie rounding differs per library (GEOS half-up, SnapToGrid half-even, C half-away, Python half-even) | Pin **half-even on the exact double product**; exact-tie golden vectors |
| 2 | scale-multiply vs grid-divide not bit-equal even in the same cell (GEOS 3.10↔3.11↔3.13) | Multiply by integer `SCALE=10^7` only; **hash int64 indices, never floats** |
| 3 | `-0.0` survives float paths into WKB | Integer lattice has no `-0` by construction; pinned by a vector |
| 4 | Consecutive-duplicate vertices removed by GEOS reducer + SnapToGrid | v2 removes coincident lattice runs (cyclically) — same shape at identity precision |
| 5 | Collapsed elements silently REMOVED (`flags=0`) → silent identity merges | v2 **REJECTS** lattice degeneracy at mint (422/GD001) |
| 6 | Topology repair adds/moves vertices; the noder is version-sensitive | v2 is pointwise-only; validity enforced at the boundary, never repaired in the hash path |
| 7 | Component ordering/orientation delegated to `GEOSNormalize` | v2 owns them as frozen spec constants (integer shoelace CCW, min-vertex rotation, byte sort) |
| 8 | Even "newer GEOS" isn't safe (3.13 reverts 3.11's change) | Zero engine calls in the identity bytes — accessors only |
| 9 | The CI image's GEOS varies by build date | Moot under v2: CI and Cloud SQL must (and do) agree bit-for-bit |

## Rejected alternatives

- **Keep v1 and pin the GEOS build forever** — impossible on managed Cloud SQL (Google controls
  the build) and merely defers the re-mint to an unplanned date.
- **In-migration re-minting of existing rows** — rejected: append-only triggers block UPDATE (the
  June-15 0004-backfill trap), `place.id` IS the PK/geoid with `predecessor_id` FKs,
  rewriting `change_log.geoid` falsifies the audit feed, and v1-distinct geometries can collide
  under v2 (an undecided collision policy). Migration 0008 instead **refuses to run on a non-empty
  registry** (both directions); the re-mint is operational (local-scripts/docs/DEPLOYMENT.md §15).
- **Silently dropping degenerate geometries or repairing them** — the v1 behavior this ADR
  classifies as a bug (silent identity merge/vanish); see ADR-006's identical reasoning on Z.
- **Tolerance/similarity matching** (GERS-style IoU) — a separate overlap-matching layer if FAO
  ever needs "same place, nearly-same shape"; never absorbed into the hash.

## Consequences

- **Every geoid re-mints.** Sanctioned and executed while the review DB is empty post-reset and
  prod is small. Old v1 geoids/permalinks are void; the re-mint runbook (export → reset →
  migrate → re-seed → **reconcile rejects**) is local-scripts/docs/DEPLOYMENT.md §15. A v1-valid geometry
  can be lattice-degenerate under v2 (422 at re-seed) — the runbook must reconcile those rows
  explicitly, never silently drop them.
- **Identity is engine-independent.** The strict/advisory golden-vector split disappears: CI, the
  deploy canary, and Cloud SQL validate the SAME digests. The canary's meaning upgrades to
  "deployed function ≡ Python reference". `--generate` is pure Python (no DB) and refuses to
  overwrite a fixture whose digests differ without `--force` — corpus regeneration is a
  recipe-version event by construction.
- **`local-scripts/rehash_geom_hashes.py` is dead post-0008** (its `pg_proc` probe fails fast —
  desirable): under a derived geoid there is no survivable re-hash, only identity-version events.
- **Dual maintenance** of the SQL and Python implementations, held together by the byte-level
  parity suite (digests AND `canonical_bytes`) over every vector, on every CI run and every deploy.
- The engine columns in `dedup_recipe_stamp` become forensics only (no longer identity-load-bearing).

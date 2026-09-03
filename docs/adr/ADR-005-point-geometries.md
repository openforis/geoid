# ADR-005 — Accept Point and MultiPoint geometries

- **Status:** Accepted — 2026-06-24. Superseded in part by
  [ADR-007](ADR-007-identity-recipe-v2.md) (recipe v2, 2026-07-02): the hash under the geoid is now
  the engine-independent integer-lattice recipe, not the frozen v1 SQL cited below — the
  point/multipoint *support* decision stands unchanged.
- **Extends:** ADR-004 (deterministic geoid; recipe frozen)
- **Implemented by:** migration `0005_support_point_geometries`, `schemas/place.SupportedGeometry`,
  `models/place` CHECK constraints, `repositories/place_repo._SUPPORTED_GEOM_TYPES`

## Context

GeoID minted geoids for **Polygon/MultiPolygon only**. The client asked to also accept **Point and
MultiPoint** geometries, deduplicated at the **same tolerance** points already share with polygon
vertices (the global `1e-7` grid). Lines (LineString/MultiLineString) and GeometryCollection are
**not** wanted and stay rejected.

The architecture is already geometry-type-agnostic almost everywhere: `place.geom` is
`geometry(Geometry, 4326)`, and the canonicalization recipe
(`ST_MakeValid → ST_ReducePrecision(1e-7) → ST_Normalize → ST_AsBinary('NDR') → sha256`), the UUIDv8
derivation (ADR-004), the read/serialization path, the WKT codec, and the output schema all operate on
any geometry type. Polygon-only was enforced at just four points: the `PlaceCreate` schema
discriminator, the ORM CHECK, the authoritative DB CHECK, and the 422 error-message type set. So this
is a **gate relaxation**, not a feature build.

## Decision

Accept **Point** and **MultiPoint**; keep **lines and GeometryCollection rejected**. **Reuse the frozen
v1 recipe verbatim** — points flow through the identical canonicalization and the same
`geoid_id_default`. No recipe change, no new recipe version, no `dedup_recipe_stamp` row, **zero risk to
existing polygon identity**. A point with sub-`1e-7` jitter dedups to the same geoid exactly as a
polygon vertex does (golden vector `point_subgrid_jitter`).

Because the recipe is unchanged, the line-direction normalization and degenerate-collapse concerns that
*lines* would have raised do not apply here, which is part of why lines remain out of scope.

### MultiPoint component ordering

A MultiPoint's identity depends on whether `ST_Normalize` canonicalizes component order. This was
**pinned empirically** during implementation by the golden-vector generator's `same_as` self-check
(`multipoint_canonical` vs `multipoint_order_swapped` on the Cloud SQL stack, PostGIS 3.6.0 / GEOS
3.11.4): `ST_Normalize` **collapses component order**, so `MULTIPOINT(5 5,0 0)` and
`MULTIPOINT(0 0,5 5)` mint the same geoid. Either way the frozen recipe is reused — the pin only
records which behavior holds.

### Empty MultiPoint

RFC 7946 sets no minimum for MultiPoint, so `{"type":"MultiPoint","coordinates":[]}` passes
geojson-pydantic → `MULTIPOINT EMPTY` → a constant per-type WKB that would silently dedup unrelated
rows onto one geoid. Guarded in two places: a `PlaceCreate` emptiness check (clean 422 with a friendly
message) and the authoritative `ck_place_geom_not_empty` DB CHECK (23514 → 422 backstop, which also
retroactively closes the same hole for polygons).

### Rejected alternatives

- **Accept lines too** — out of scope; lines reintroduce direction-normalization and degenerate-collapse
  questions the point case avoids, and the client did not ask for them.
- **A new recipe version / per-type tolerance** — unnecessary; the client asked for the *same*
  tolerance, and a recipe change would be an identity-version event risking every existing geoid.

## Consequences

- **(+)** Point/MultiPoint mint content-addressed UUIDv8 geoids and dedup identically to polygons; same
  geometry within `1e-7` → same geoid → 409 + incumbent.
- **(=)** Two pre-existing, out-of-scope behaviors carry over **unchanged** from polygons. (1) **3D (Z)
  coordinates are not supported**: a geometry carrying a Z ordinate is rejected explicitly at the Python
  validator (GeoID is 2D-only) with a clear **422**, the same trust boundary that rejects lines/
  GeometryCollection — stack-independent and unit-tested (no longer the prod-only, untestable DB artifact
  it once was; see [ADR-006](ADR-006-2d-only.md)). (2) A geometry whose vertices all collapse under
  `1e-7` to nothing would
  canonicalize to empty — now blocked for every type by `ck_place_geom_not_empty`.
- **(=)** No identity event: existing polygon geoids are byte-for-byte unchanged; the golden corpus only
  gains appended point vectors at `recipe_version "v1"`.

# ADR-006 — Reject 3D (Z) coordinates at the validator: GeoID is 2D-only

- **Status:** Accepted — 2026-06-24. Superseded in part by
  [ADR-007](ADR-007-identity-recipe-v2.md) (recipe v2, 2026-07-02): the GEOS-bound recipe described
  in the Context below is replaced by the engine-independent integer-lattice recipe — the 2D-only
  rule itself stands unchanged (and ADR-007's degeneracy rejection reuses its rationale).
- **Extends:** ADR-004 (deterministic geoid; recipe frozen),
  [ADR-005](ADR-005-point-geometries.md) (point/multipoint support)
- **Implemented by:** `schemas/place._has_z` + the `_validate_lonlat_bounds` 2D-only check
- **Reverses:** the `WktCodec` docstring's implicit "keep Z, don't fix it" stance (now: the codec keeps
  Z for format fidelity, the schema rejects it)

## Context

A geometry matrix found 3D/Z geometries were **rejected on the deployed stack** (PostGIS 3.6 / GEOS
3.11.4) with a **422 "unparseable GeoJSON geometry"**, while the local CI stack (PostGIS 3.5 / GEOS
3.9.0) **accepted** them.

- **RFC 7946 (§3.1.1, §4)** permits an optional 3rd ordinate (altitude) but makes honoring it
  **application-specific** — a 2D footprint gazetteer that ignores altitude is fully compliant.
- `ST_GeomFromGeoJSON` parses 3D fine, so the "unparseable" label was **wrong**. The real failure was
  the recipe's `ST_ReducePrecision` (GEOS `GEOSGeom_setPrecision`), which is **XY-only and
  version-sensitive on Z** — hence prod errored and local didn't.

So 2D-only is a correct, standards-defensible policy. But the *old rejection mechanism* was the
oversight: accidental (a GEOS bug, not a rule), **stack-divergent**, **mislabeled**, never enforced at
the trust boundary, and a **latent identity hazard** — on a stack that accepts Z, `POINT(0 0 5)` and
`POINT(0 0 99)` would mint different geoids than `POINT(0 0)`, and the recipe is identity-load-bearing
(ADR-004).

## Decision

Reject any Z ordinate **at the Python validator** (`schemas/place`), the same trust boundary that
already rejects lines/GeometryCollection, with an **honest 422** message. This converts a prod-only,
untestable artifact into a deterministic, locally unit-testable rule. The frozen recipe is untouched;
**no migration**. The `WktCodec` still keeps Z for format fidelity (it is a faithful converter, not a
mutator); rejection happens one layer up, uniform for the GeoJSON and WKT input paths.

## Rejected alternative

- **Silently strip Z** (`force_2d`) — declined. An identity service must not silently mutate identity
  input: stripping would merge `POINT(0 0 5)`, `POINT(0 0 99)`, and `POINT(0 0)` onto one geoid without
  the caller knowing their altitude was discarded. Rejecting is honest; the caller strips and resubmits.

## Consequences

- **(+)** 2D-only is now deliberate, stack-independent, and unit-tested — the assertion
  `test_3d_point_rejected_422` that was previously impossible (stack-divergent) now passes on the local
  stack because rejection is pre-DB.
- **(+)** Honest 422 message ("3D (Z) coordinates are not supported: GeoID is 2D-only …") instead of the
  misleading "unparseable GeoJSON geometry".
- **(=)** No identity change, no migration, no recipe change. Existing geoids are untouched.

"""identity recipe v2: engine-independent integer-lattice canonicalization (ADR-007)

Recipe v1 delegated canonicalization to GEOS (``ST_MakeValid`` → ``ST_ReducePrecision``
→ ``ST_Normalize`` → WKB bytes), so the geoid — derived from the hash since migration
0004 — depended on the deployed GEOS build. That dependence is real, demonstrated
drift: GEOS 3.9/3.10 round sub-unit grids as ``round(v * (1/grid))``, 3.11 switched to
``round(v / grid) * grid`` (JTS PR #804), and 3.13 reverted for sub-unit grids — three
incompatible behaviors for the same input, plus reconstructed-double bit mismatches
even when both land in the same lattice cell. v2 removes every engine call from the
identity bytes (senior-review finding H5, approved 2026-07-02):

    quantize:   q = round_half_even(coord × 10000000.0)      -- ONE IEEE-754 multiply
                (PG float8→bigint cast = rint = ties-to-even; SCALE 10^7 is exactly
                representable as a float64, 1e-7 is not — multiply, never divide)
    ring:       cyclic consecutive-duplicate removal → REJECT if < 3 vertices or
                exact integer shoelace = 0 (SQLSTATE GD001) → orient CCW → rotate
                the lexicographically smallest (x, y) vertex to front
    structure:  exterior first + holes sorted by canonical bytes; MultiPolygon
                bodies sorted by canonical bytes; MultiPoint members sorted by
                (x, y) with duplicates kept
    serialize:  0x02 || type_tag || body, big-endian int4 counts / int8 lattice
                indices — int64 lattice indices are hashed, never reconstructed
                floats (kills reverse-scale bit drift and IEEE -0.0 by construction)
    geom_hash:  sha256(canonical_bytes)

Only accessors touch the geometry (ST_DumpPoints / ST_X / ST_Y / ST_ExteriorRing /
ST_InteriorRingN / ST_GeometryN / GeometryType — all liblwgeom-native, GEOS-free);
``domain/geometry_identity.py`` is the byte-identical pure-Python reference, pinned
by the full-corpus parity suite (scripts/data/dedup_golden_vectors_v2.json).

There is deliberately NO repair leg (v1's ``ST_MakeValid`` is dropped): validity is a
boundary concern (schema 422 + ``ck_place_geom_is_valid``), and v1's VALID_OUTPUT mode
silently emptied collapsed slivers — a silent identity merge. v2 REJECTS lattice
degeneracy instead (GD001 → 422), mirrored early by the PlaceCreate schema pre-check.
There is also no grid parameter — v1's ``geoid_geom_hash(g, grid)`` generality is
dropped (a retune is a migration event); the function is DROPPED so no GEOS-bound
hash stays callable on an identity database.

THIS IS A SANCTIONED IDENTITY-VERSION EVENT: the same geometry mints a DIFFERENT
geoid under v2 than under v1, so this migration REFUSES to run on a non-empty
registry. Executed while the review DB is empty post-reset and prod is small —
the re-mint runbook is local-scripts/docs/DEPLOYMENT.md §15 (export → reset → migrate →
re-seed → reconcile rejects). ``geoid_from_geom_hash`` / ``geoid_id_default``
(the UUIDv8 stamping, 0004) are UNCHANGED — only the hash under them changes.

Revision ID: 0008_identity_recipe_v2
Revises: 0007_hardening
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_identity_recipe_v2"
down_revision: str | None = "0007_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both directions re-mint identity, so both refuse a non-empty registry. In-migration
# re-minting was REJECTED by design: the append-only triggers block UPDATE (the
# June-15 0004-backfill trap), place.id IS the PK/geoid with predecessor_id FKs,
# rewriting change_log.geoid would falsify the audit feed, and v1-distinct geometries
# can collide under v2 (a collision policy nobody has decided) — all that machinery
# for a sanctioned-empty review DB and a small, exportable prod.
_EMPTY_GUARD = """
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM place) OR EXISTS (SELECT 1 FROM geoid_registry) THEN
        RAISE EXCEPTION 'identity recipe migration refuses to run: place/geoid_registry are not empty. '
            'Changing the recipe re-mints EVERY geoid — follow the re-mint runbook '
            '(local-scripts/docs/DEPLOYMENT.md, section 15): export -> reset the DB -> migrate -> '
            're-seed -> reconcile rejects.';
    END IF;
END
$guard$;
"""


def upgrade() -> None:
    op.execute(_EMPTY_GUARD)

    # --- geoid_quantize_v2(float8) -> bigint --------------------------------
    # ONE IEEE-754 double multiply by the integer SCALE (10^7, exactly
    # representable), then the float8->bigint cast (dtoi8 = C rint = ties-to-
    # even). The ::float8 on the literal is load-bearing: a numeric path would
    # round ties away-from-zero. Mirrors Python round(coord * 10000000.0).
    op.execute(
        """
        CREATE FUNCTION geoid_quantize_v2(c double precision)
        RETURNS bigint
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT (c * 10000000.0::float8)::bigint;
        $func$;
        """
    )

    # --- geoid_canon_ring_v2(linestring) -> bytea ----------------------------
    # One ring (from ST_ExteriorRing / ST_InteriorRingN) to its canonical bytes:
    # int4 count || count x (int8 qx || int8 qy). Collinear vertices are NEVER
    # removed (an inserted midpoint stays detectable); only coincident lattice
    # runs collapse. Degeneracy RAISEs SQLSTATE GD001 — deliberately NOT 23514,
    # whose handler asks ST_IsValidReason (it answers "Valid Geometry" for a
    # lattice-degenerate sliver).
    op.execute(
        """
        CREATE FUNCTION geoid_canon_ring_v2(ring geometry)
        RETURNS bytea
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
        DECLARE
            xs bigint[];
            ys bigint[];
            n int;
            area numeric;
            min_i int;
            starts int[];
            best int;
            pos_a int;
            pos_b int;
            cnd int;
            step int;
            result bytea;
        BEGIN
            -- Quantize every listed vertex, drop consecutive duplicates (forward pass).
            SELECT array_agg(x ORDER BY i), array_agg(y ORDER BY i)
              INTO xs, ys
              FROM (
                  SELECT i, x, y,
                         lag(x) OVER (ORDER BY i) AS px,
                         lag(y) OVER (ORDER BY i) AS py
                  FROM (
                      SELECT dp.path[1] AS i,
                             geoid_quantize_v2(ST_X(dp.geom)) AS x,
                             geoid_quantize_v2(ST_Y(dp.geom)) AS y
                      FROM ST_DumpPoints(ring) AS dp
                  ) q
              ) w
             WHERE px IS NULL OR x <> px OR y <> py;

            -- Wrap-around trim (cyclic dedup): subsumes the closing vertex, so no
            -- step depends on the parser's closure representation.
            n := coalesce(array_length(xs, 1), 0);
            WHILE n > 1 AND xs[n] = xs[1] AND ys[n] = ys[1] LOOP
                n := n - 1;
            END LOOP;

            IF n < 3 THEN
                RAISE EXCEPTION 'geometry degenerates at the identity precision (1e-7 deg): ring collapses to % distinct lattice vertices', n
                    USING ERRCODE = 'GD001';
            END IF;

            -- Exact integer shoelace (2x signed lattice area), accumulated in
            -- numeric: a 3-term product sum can overflow int64 at world extent.
            SELECT sum(xs[gs]::numeric * ys[gs % n + 1]::numeric
                       - xs[gs % n + 1]::numeric * ys[gs]::numeric)
              INTO area
              FROM generate_series(1, n) AS gs;
            IF area = 0 THEN
                RAISE EXCEPTION 'geometry degenerates at the identity precision (1e-7 deg): ring has zero lattice area'
                    USING ERRCODE = 'GD001';
            END IF;
            IF area < 0 THEN
                -- Orient every ring CCW (its role is positional, not encoded).
                SELECT array_agg(xs[gs] ORDER BY gs DESC), array_agg(ys[gs] ORDER BY gs DESC)
                  INTO xs, ys
                  FROM generate_series(1, n) AS gs;
            END IF;

            -- Rotate the lexicographically smallest (x, y) vertex to front.
            SELECT gs INTO min_i
              FROM generate_series(1, n) AS gs
             ORDER BY xs[gs], ys[gs], gs
             LIMIT 1;
            SELECT array_agg(gs) INTO starts
              FROM generate_series(1, n) AS gs
             WHERE xs[gs] = xs[min_i] AND ys[gs] = ys[min_i];

            -- Tie-break (a lattice pinch repeats the minimum): pick the rotation
            -- with the smallest vertex sequence — element-wise integer compare,
            -- identical to the Python mirror's tuple comparison.
            best := starts[1];
            FOR cnd IN 2 .. array_length(starts, 1) LOOP
                FOR step IN 0 .. n - 1 LOOP
                    pos_a := (starts[cnd] - 1 + step) % n + 1;
                    pos_b := (best - 1 + step) % n + 1;
                    IF xs[pos_a] <> xs[pos_b] OR ys[pos_a] <> ys[pos_b] THEN
                        IF xs[pos_a] < xs[pos_b]
                           OR (xs[pos_a] = xs[pos_b] AND ys[pos_a] < ys[pos_b]) THEN
                            best := starts[cnd];
                        END IF;
                        EXIT;
                    END IF;
                END LOOP;
            END LOOP;

            -- Serialize: big-endian int4 count, then int8 lattice pairs
            -- (int4send/int8send = two's-complement big-endian = struct '>i'/'>q').
            SELECT int4send(n)
                   || string_agg(int8send(xs[(best - 1 + gs) % n + 1])
                                 || int8send(ys[(best - 1 + gs) % n + 1]),
                                 ''::bytea ORDER BY gs)
              INTO result
              FROM generate_series(0, n - 1) AS gs;
            RETURN result;
        END;
        $func$;
        """
    )

    # --- geoid_canon_polygon_body_v2(polygon) -> bytea -----------------------
    # int4 nrings || exterior || holes sorted by canonical bytes (bytea memcmp
    # = Python bytes ordering; no collation anywhere).
    op.execute(
        """
        CREATE FUNCTION geoid_canon_polygon_body_v2(g geometry)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT int4send(ST_NumInteriorRings(g) + 1)
                || geoid_canon_ring_v2(ST_ExteriorRing(g))
                || coalesce(
                       (SELECT string_agg(rb, ''::bytea ORDER BY rb)
                          FROM (SELECT geoid_canon_ring_v2(ST_InteriorRingN(g, gs)) AS rb
                                  FROM generate_series(1, ST_NumInteriorRings(g)) AS gs) holes),
                       ''::bytea);
        $func$;
        """
    )

    # --- geoid_canonical_bytes_v2(geometry) -> bytea --------------------------
    # The dispatcher: 0x02 version byte || type tag || body. Empty and
    # unsupported-type inputs raise with ERRCODE 23514 (check_violation) so the
    # existing write-path handler recovers the reason exactly as it did when the
    # place CHECKs fired first; lattice degeneracy is the only GD001 class.
    op.execute(
        r"""
        CREATE FUNCTION geoid_canonical_bytes_v2(g geometry)
        RETURNS bytea
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
        DECLARE
            gtype text := GeometryType(g);
        BEGIN
            IF ST_IsEmpty(g) THEN
                RAISE EXCEPTION 'geometry is empty: it carries no coordinates'
                    USING ERRCODE = 'check_violation';
            END IF;
            CASE gtype
                WHEN 'POINT' THEN
                    RETURN '\x0201'::bytea
                        || int8send(geoid_quantize_v2(ST_X(g)))
                        || int8send(geoid_quantize_v2(ST_Y(g)));
                WHEN 'MULTIPOINT' THEN
                    -- Members sorted by (x, y), duplicates KEPT (no silent merge).
                    RETURN (
                        SELECT '\x0202'::bytea || int4send(count(*)::int)
                            || string_agg(int8send(qx) || int8send(qy), ''::bytea ORDER BY qx, qy)
                          FROM (SELECT geoid_quantize_v2(ST_X(dp.geom)) AS qx,
                                       geoid_quantize_v2(ST_Y(dp.geom)) AS qy
                                  FROM ST_DumpPoints(g) AS dp) pts);
                WHEN 'POLYGON' THEN
                    RETURN '\x0203'::bytea || geoid_canon_polygon_body_v2(g);
                WHEN 'MULTIPOLYGON' THEN
                    RETURN (
                        SELECT '\x0204'::bytea || int4send(ST_NumGeometries(g))
                            || string_agg(pb, ''::bytea ORDER BY pb)
                          FROM (SELECT geoid_canon_polygon_body_v2(ST_GeometryN(g, gs)) AS pb
                                  FROM generate_series(1, ST_NumGeometries(g)) AS gs) parts);
                ELSE
                    RAISE EXCEPTION 'unsupported geometry type for identity: %', gtype
                        USING ERRCODE = 'check_violation';
            END CASE;
        END;
        $func$;
        """
    )

    # --- geoid_geom_hash_v2(geometry) -> bytea --------------------------------
    op.execute(
        """
        CREATE FUNCTION geoid_geom_hash_v2(g geometry)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT digest(geoid_canonical_bytes_v2(g), 'sha256');
        $func$;
        """
    )

    # --- Flip the ONE entry point ---------------------------------------------
    # The arbiter CTE, the incumbent lookup, and geoid_id_default all call
    # geoid_geom_hash_default — re-pointing it flips dedup AND identity together.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_geom_hash_default(g geometry)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT geoid_geom_hash_v2(g);
        $func$;
        """
    )

    # --- Retire the GEOS-bound v1 recipe ---------------------------------------
    # Nothing shipped calls it post-flip; leaving a live GEOS-bound hash callable
    # on an identity database invites exactly the drift class v2 removes (and
    # rehash_geom_hashes.py's pg_proc probe now fails fast — desirable).
    op.execute("DROP FUNCTION geoid_geom_hash(geometry, double precision);")

    # --- Record the recipe-version event ---------------------------------------
    # The engine columns are forensics only from here on (identity no longer
    # depends on the PostGIS/GEOS stack) but stay NOT NULL bookkeeping.
    op.execute(
        """
        INSERT INTO dedup_recipe_stamp
            (recipe_version, postgis_version, geos_version, postgis_full,
             stamped_by, note)
        SELECT 'v2', postgis_lib_version(), postgis_geos_version(),
               postgis_full_version(), 'migration:0008',
               'identity recipe v2 (ADR-007): engine-independent integer-lattice '
               'canonicalization — sha256 over big-endian int64 lattice indices '
               '(round_half_even(coord*1e7)), structure canonicalized by frozen spec '
               'constants, no GEOS/PostGIS call in the identity bytes. Identity-version '
               'event: every geoid re-mints; applied to an EMPTY registry only. The '
               'engine versions on this row are forensics, no longer identity-load-bearing.';
        """
    )


def downgrade() -> None:
    op.execute(_EMPTY_GUARD)

    # Restore the v1 pair verbatim (bodies from 0001, attributes from 0007).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_geom_hash(g geometry, grid double precision)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT digest(
                ST_AsBinary(
                    ST_Normalize(ST_ReducePrecision(ST_MakeValid(g), grid)),
                    'NDR'
                ),
                'sha256'
            );
        $func$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_geom_hash_default(g geometry)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT geoid_geom_hash(g, 1e-7);
        $func$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS geoid_geom_hash_v2(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_canonical_bytes_v2(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_canon_polygon_body_v2(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_canon_ring_v2(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_quantize_v2(double precision);")
    op.execute(
        """
        INSERT INTO dedup_recipe_stamp
            (recipe_version, postgis_version, geos_version, postgis_full,
             stamped_by, note)
        SELECT 'v1', postgis_lib_version(), postgis_geos_version(),
               postgis_full_version(), 'migration:0008-downgrade',
               'identity recipe v2 rolled back to v1 (GEOS-bound) on an empty registry.';
        """
    )

"""deterministic, content-addressed geoid: UUIDv8 derived from the geometry hash

Before this revision the geoid (``place.id`` / ``geoid_registry.geoid``) was a random
UUIDv7 minted app-side, independent of the geometry. From here the geoid is a
**deterministic UUIDv8** (RFC 9562 §5.8) derived from the geometry's canonical SHA-256
``geom_hash`` — the same fingerprint already used for dedup:

    geoid = first 16 bytes of geoid_geom_hash_default(geom), with the version (8) and
            RFC 4122 variant bits stamped in place.

So identity becomes a pure function of the canonical geometry: the same geometry mints
the same geoid on every deployment (federation without coordination) and a re-created
geometry recovers its geoid. RFC 9562 §6.5 mandates UUIDv8 (not the SHA-1 v5) for
SHA-256-based name UUIDs; we reuse ``geom_hash`` rather than hashing a second time.

Two IMMUTABLE SQL functions are added (mirroring the ``geoid_geom_hash`` /
``geoid_geom_hash_default`` pair so app + scripts call one wrapper):

- ``geoid_from_geom_hash(bytea) -> uuid``   stamp the version/variant bits onto the
  first 16 bytes of a 32-byte digest. Byte-identical to the Python
  ``geoid.domain.identifiers.geoid_from_geom_hash``.
- ``geoid_id_default(geometry) -> uuid``    = ``geoid_from_geom_hash(geoid_geom_hash_default(g))``;
  the single entry point the arbiter CTE (``repositories/place_repo.insert_place``) uses
  to compute the geoid DB-side, so identity and dedup are derived from one hash and can
  never drift.

CONSEQUENCE (recorded in an appended ``dedup_recipe_stamp`` row): the canonicalization
recipe (grid ``1e-7`` + the PostGIS/GEOS output bytes) is now **identity-load-bearing**,
not just dedup bookkeeping. A recipe or GEOS change that shifts a canonical vertex would
re-mint geoids and break permalinks, so the recipe is frozen for all time — a future
change is an *identity-version* event, NOT a survivable ``rehash_geom_hashes.py`` re-hash.

No new tables, triggers, or ORM models: the stamp row uses the existing
``dedup_recipe_stamp`` ``note`` column (the user-trigger inventory stays at 7, D1).

Revision ID: 0004_deterministic_geoid
Revises: 0003_dedup_recipe_stamp
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_deterministic_geoid"
down_revision: str | None = "0003_dedup_recipe_stamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- geoid_from_geom_hash(bytea) -> uuid -------------------------------
    # Stamp the UUIDv8 version (high nibble of byte 6 = 0x8) and the RFC 4122
    # variant (high bits of byte 8 = 0b10) onto the first 16 bytes of the digest.
    # get_byte/set_byte are 0-indexed; (b & 15) | 128 sets the version nibble,
    # (b & 63) | 128 sets the variant bits. encode(...,'hex')::uuid parses the
    # 32-hex-digit (hyphen-free) form. Must match domain/identifiers.py exactly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_from_geom_hash(h bytea)
        RETURNS uuid
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $func$
            SELECT encode(
                set_byte(
                    set_byte(
                        substring(h FROM 1 FOR 16),
                        6, (get_byte(h, 6) & 15) | 128
                    ),
                    8, (get_byte(h, 8) & 63) | 128
                ),
                'hex'
            )::uuid;
        $func$;
        """
    )

    # --- geoid_id_default(geometry) -> uuid --------------------------------
    # The one entry point that turns a geometry into its geoid, derived from the
    # SAME grid-pinned hash wrapper used for dedup — so the arbiter CTE computes
    # both the geoid and geom_hash from one canonical fingerprint.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_id_default(g geometry)
        RETURNS uuid
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $func$
            SELECT geoid_from_geom_hash(geoid_geom_hash_default(g));
        $func$;
        """
    )

    # --- Record the identity-derivation event in the recipe stamp ----------
    # recipe_version stays 'v1' (the hash SQL is unchanged); the note records that
    # the recipe is now identity-load-bearing and therefore frozen.
    op.execute(
        """
        INSERT INTO dedup_recipe_stamp
            (recipe_version, postgis_version, geos_version, postgis_full,
             stamped_by, note)
        SELECT 'v1', postgis_lib_version(), postgis_geos_version(),
               postgis_full_version(), 'migration:0004',
               'geoid identity is now derived from this recipe: '
               'geoid = UUIDv8(first 16 bytes of geoid_geom_hash_default(geom)). '
               'The canonicalization (grid 1e-7 + GEOS stack) is now identity-load-bearing; '
               'a recipe/GEOS change re-mints geoids, so it is frozen (an identity-version '
               'event), not a survivable rehash.';
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS geoid_id_default(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_from_geom_hash(bytea);")

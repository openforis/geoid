"""DB hardening: grant-email normalization CHECK, PARALLEL SAFE + pinned
search_path on the four identity/dedup functions, drop the dead paging index

Hardenings from the 2026-07-01 review, one migration:

- 2D-only (M3) needs NO new constraint — resolved by inspection: the ``place.geom``
  column type ``geometry(Geometry, 4326)`` is a 2D typmod, so PostGIS itself
  rejects any Z/M-bearing write ("Geometry has Z dimension but column does not")
  at the column-type level, below any app code. The review's premise (2D-only held
  only in the ``_has_z`` Pydantic validator) was wrong; an integration test now
  pins the typmod rejection instead of duplicating it as a CHECK that could never
  fire.
- ``ck_collection_grant_email_normalized`` (M4): grants are keyed by a normalised
  (strip+lower) email, but only ``grant_repo._norm`` enforced it — a bypassing write
  could create case-duplicate grants for one person. The CHECK makes the invariant
  schema-held.
- The four SQL functions (``geoid_geom_hash``, ``geoid_geom_hash_default``,
  ``geoid_from_geom_hash``, ``geoid_id_default``) are re-created with ``PARALLEL
  SAFE`` (default is UNSAFE, which bars future large audit/verification scans from
  parallel plans) and ``SET search_path = pg_catalog, public`` (resolution
  hardening). **The bodies are byte-identical to 0001/0004** — the recipe is
  identity-load-bearing and frozen; only the function *attributes* change, so every
  stored hash and geoid is untouched (the golden-vector suite pins this).
- ``place_collection_created_id_idx`` (M17): the 0002 paging index backed the
  removed items-listing surface; no surviving query uses it and ``place`` is
  INSERT-only, so it is pure write amplification. 0002 itself is applied history
  and stays untouched; the drop ships here. Plain (non-CONCURRENT) DDL throughout:
  current tables are tiny and this runs in the blocking migrate job.

NO ``dedup_recipe_stamp`` row: recipe SQL, grid, and GEOS stack are all unchanged.

Revision ID: 0007_hardening
Revises: 0006_grants
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_hardening"
down_revision: str | None = "0006_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- M4: grant emails are stored normalised, now schema-held ------------
    op.execute(
        "ALTER TABLE collection_grant ADD CONSTRAINT ck_collection_grant_email_normalized "
        "CHECK (principal_email = lower(btrim(principal_email)));"
    )

    # --- M6 + search_path pin: same bodies, hardened attributes -------------
    # Bodies MUST stay byte-identical to 0001/0004 (frozen v1 recipe). Only
    # PARALLEL SAFE and the pinned search_path are added.
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_from_geom_hash(h bytea)
        RETURNS uuid
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_id_default(g geometry)
        RETURNS uuid
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $func$
            SELECT geoid_from_geom_hash(geoid_geom_hash_default(g));
        $func$;
        """
    )

    # --- M17: drop the dead 0002 paging index --------------------------------
    op.execute("DROP INDEX IF EXISTS place_collection_created_id_idx;")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX place_collection_created_id_idx ON place (collection_id, created_at, id);"
    )
    # Restore the 0001/0004 function attributes (bodies identical either way).
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_geom_hash_default(g geometry)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $func$
            SELECT geoid_geom_hash(g, 1e-7);
        $func$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_geom_hash(g geometry, grid double precision)
        RETURNS bytea
        LANGUAGE sql
        IMMUTABLE
        STRICT
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
    op.execute("ALTER TABLE collection_grant DROP CONSTRAINT ck_collection_grant_email_normalized;")

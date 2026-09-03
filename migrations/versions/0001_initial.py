"""initial schema: catalog/collection/place + geoid_registry + change_log,
the canonical geom_hash recipe + its grid-pinned default wrapper, and the
change_log-population / immutability / append-only triggers. The global
geometry-dedup UNIQUE lives on geoid_registry (sharding-ready), written by the
app's arbiter CTE — not by a trigger.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Extensions --------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # --- catalog (STAC catalog) --------------------------------------------
    op.execute(
        """
        CREATE TABLE catalog (
            id        uuid PRIMARY KEY,
            slug      text UNIQUE NOT NULL,
            title     text,
            metadata  jsonb NOT NULL DEFAULT '{}'::jsonb
        );
        """
    )

    # --- collection (≈ STAC collection) -----------------------------------
    op.execute(
        """
        CREATE TABLE collection (
            id             uuid PRIMARY KEY,
            catalog_id     uuid NOT NULL REFERENCES catalog(id),
            slug           text NOT NULL,
            title          text,
            writable_anon  boolean NOT NULL DEFAULT false,
            metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT uq_collection_catalog_slug UNIQUE (catalog_id, slug)
        );
        """
    )

    # --- place (≈ STAC item) — INSERT-only; id IS the geoid (UUIDv7) -------
    # The dedup geom_hash and its GLOBAL UNIQUE do NOT live here — they live on
    # geoid_registry (below), so place can be partitioned/Citus-sharded by
    # collection_id without an impossible cross-shard UNIQUE. The app's arbiter
    # CTE writes the registry row first and only then this place row.
    op.execute(
        """
        CREATE TABLE place (
            id                    uuid PRIMARY KEY,
            collection_id         uuid NOT NULL REFERENCES collection(id),
            geom                  geometry(Geometry, 4326) NOT NULL,
            external_id           text,
            provenance            jsonb NOT NULL DEFAULT '{}'::jsonb,
            predecessor_id        uuid REFERENCES place(id),
            created_at            timestamptz NOT NULL DEFAULT now(),
            originating_instance  text,
            CONSTRAINT ck_place_geom_is_polygonal
                CHECK (GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')),
            CONSTRAINT ck_place_geom_is_valid
                CHECK (ST_IsValid(geom)),
            CONSTRAINT uq_place_collection_external_id
                UNIQUE (collection_id, external_id)
        );
        """
    )

    # --- HINGE 1: geoid_registry (global uniqueness, Citus-shard ready) ----
    # Holds BOTH global uniqueness invariants on one unpartitioned/reference
    # table: geoid uniqueness (PK) and the GLOBAL geometry-dedup UNIQUE
    # (uq_geoid_registry_geom_hash on geom_hash). Keeping geom_hash here — not on
    # place — is what makes the catalog sharding-ready: PostgreSQL/Citus require a
    # UNIQUE to include the partition/distribution column, so a global
    # UNIQUE(geom_hash) on a place sharded by collection_id is impossible, but a
    # reference-table registry stays globally unique.
    #
    # Populated by the app's arbiter CTE (registry_service / place_repo), NOT a
    # trigger: the CTE inserts the registry row (ON CONFLICT DO NOTHING on the
    # geom_hash UNIQUE) and writes the place row ONLY if the registry arbiter won,
    # in one statement — so every place row has its registry row, the duplicate
    # path never aborts the transaction, and geometry precedence over external_id
    # is preserved. INTENTIONALLY no FK to place: this table is destined to become
    # a Citus REFERENCE table (replicated to every node) once place is distributed
    # by collection_id, and reference->distributed FKs are not supported; a FK
    # would also serialize cross-shard inserts at scale. Integrity is held by the
    # single-statement CTE (sole writer) + the append-only guards.
    op.execute(
        """
        CREATE TABLE geoid_registry (
            geoid          uuid PRIMARY KEY,
            place_id       uuid NOT NULL,
            collection_id  uuid NOT NULL,
            geom_hash      bytea NOT NULL,
            -- One geometry → one geoid across the WHOLE catalog (global dedup):
            -- the arbiter insert hits this and DOES NOTHING, so the API answers
            -- 409 + the incumbent geoid (looked up catalog-wide by geom_hash).
            CONSTRAINT uq_geoid_registry_geom_hash
                UNIQUE (geom_hash)
        );
        """
    )

    # --- HINGE 2: change_log (append-only audit / federation pull-feed) ----
    # Same deliberate FK omission as geoid_registry: the feed must stay
    # consumable (and its rows immutable) even after place is sharded or rows
    # are served from another federation instance that this DB never stores.
    op.execute(
        """
        CREATE TABLE change_log (
            seq            bigserial PRIMARY KEY,
            geoid          uuid NOT NULL,
            op             text NOT NULL,
            collection_id  uuid NOT NULL,
            payload        jsonb,
            ts             timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # --- Stubbed seams (tables only for the demo) --------------------------
    op.execute(
        """
        CREATE TABLE authority_assertion (
            id              uuid PRIMARY KEY,
            geoid           uuid NOT NULL,
            asserted_by     text,
            authority_type  text,
            claim           jsonb,
            created_at      timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE api_key (
            id          uuid PRIMARY KEY,
            key_hash    text,
            principal   text,
            scopes      jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            revoked_at  timestamptz
        );
        """
    )

    # --- The canonical dedup recipe — ONE definition, wrapped by
    #     geoid_geom_hash_default (below) and used by the arbiter insert, the
    #     incumbent-lookup query, and local-scripts/rehash_geom_hashes.py. -----------
    # geom_hash = sha256( ST_AsBinary(
    #               ST_Normalize( ST_ReducePrecision( ST_MakeValid(geom), grid ) ),
    #               'NDR' ) )
    # 'NDR' endianness is PINNED so a country instance's hash matches central at sync.
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

    # --- The ONE global dedup grid, pinned as a SQL wrapper -----------------
    # geoid_geom_hash_default(geom) = geoid_geom_hash(geom, 1e-7). 1e-7 deg ≈
    # 1cm/vertex — exact-match semantics (the grid absorbs float jitter only, it
    # does not merge nearby shapes). The grid literal is pinned HERE so the app's
    # arbiter insert, the incumbent lookup, and local-scripts/rehash_geom_hashes.py all
    # call this wrapper and no grid ever crosses from config;
    # GEOID_DEDUP_GRID_DEFAULT must mirror this literal (a unit test pins the two
    # equal), so retuning is a migration event, never a config-only change.
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

    # --- AFTER INSERT: append the change_log feed --------------------------
    # The geoid_registry hinge is written by the app's arbiter CTE (it is the
    # dedup arbiter and must run BEFORE the place insert), so this trigger only
    # appends the change_log audit / federation pull-feed row.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION place_after_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $func$
        BEGIN
            INSERT INTO change_log (geoid, op, collection_id, payload)
            VALUES (
                NEW.id,
                'create',
                NEW.collection_id,
                jsonb_build_object(
                    'external_id', NEW.external_id,
                    'originating_instance', NEW.originating_instance
                )
            );
            RETURN NEW;
        END;
        $func$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER place_after_insert_ai
        AFTER INSERT ON place
        FOR EACH ROW EXECUTE FUNCTION place_after_insert();
        """
    )

    # --- Immutability: block UPDATE/DELETE on place -------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION place_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $func$
        BEGIN
            RAISE EXCEPTION
                'place is immutable: % is not allowed (corrections mint a new geoid via predecessor_id)',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $func$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER place_block_mutation_bud
        BEFORE UPDATE OR DELETE ON place
        FOR EACH ROW EXECUTE FUNCTION place_block_mutation();
        """
    )
    # TRUNCATE bypasses row-level triggers, so block it explicitly (statement-level)
    # to keep the "INSERT-only" guarantee whole.
    op.execute(
        """
        CREATE TRIGGER place_block_truncate_bt
        BEFORE TRUNCATE ON place
        FOR EACH STATEMENT EXECUTE FUNCTION place_block_mutation();
        """
    )

    # --- Append-only enforcement for the two hinges ------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION geoid_append_only_block()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $func$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $func$;
        """
    )
    for table in ("geoid_registry", "change_log"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only_bud
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION geoid_append_only_block();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only_bt
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION geoid_append_only_block();
            """
        )

    # --- Spatial index (reads + future ST_Intersects) ----------------------
    op.execute("CREATE INDEX place_geom_gix ON place USING gist (geom);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS place_geom_gix;")
    op.execute("DROP TRIGGER IF EXISTS change_log_append_only_bt ON change_log;")
    op.execute("DROP TRIGGER IF EXISTS change_log_append_only_bud ON change_log;")
    op.execute("DROP TRIGGER IF EXISTS geoid_registry_append_only_bt ON geoid_registry;")
    op.execute("DROP TRIGGER IF EXISTS geoid_registry_append_only_bud ON geoid_registry;")
    op.execute("DROP FUNCTION IF EXISTS geoid_append_only_block();")
    op.execute("DROP TRIGGER IF EXISTS place_block_truncate_bt ON place;")
    op.execute("DROP TRIGGER IF EXISTS place_block_mutation_bud ON place;")
    op.execute("DROP TRIGGER IF EXISTS place_after_insert_ai ON place;")
    op.execute("DROP FUNCTION IF EXISTS place_block_mutation();")
    op.execute("DROP FUNCTION IF EXISTS place_after_insert();")
    op.execute("DROP FUNCTION IF EXISTS geoid_geom_hash_default(geometry);")
    op.execute("DROP FUNCTION IF EXISTS geoid_geom_hash(geometry, double precision);")
    op.execute("DROP TABLE IF EXISTS api_key;")
    op.execute("DROP TABLE IF EXISTS authority_assertion;")
    op.execute("DROP TABLE IF EXISTS change_log;")
    op.execute("DROP TABLE IF EXISTS geoid_registry;")
    op.execute("DROP TABLE IF EXISTS place;")
    op.execute("DROP TABLE IF EXISTS collection;")
    op.execute("DROP TABLE IF EXISTS catalog;")

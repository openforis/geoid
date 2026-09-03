"""Relax external_id uniqueness for the reserved public collection

The public default collection accepts anonymous writes and must tolerate
duplicate (or absent) external_id values: submitted values are stored and
echoed on reads, but never unique and never resolvable — the external-id
lookup answers an explicit 400 for the public collection (api/places.py
guard). Private collections keep exact per-collection uniqueness and the
409 mapping.

Replaces the table constraint ``uq_place_collection_external_id`` with a
partial UNIQUE INDEX of the SAME NAME excluding the public collection's row.
The name is load-bearing: Postgres reports a unique-index violation under the
index name, so the asyncpg constraint-name classification (UQ_PLACE_EXTERNAL_ID
-> 409) keeps working with zero code change.

Fresh DB (the migrate job runs before the service's bootstrap): the public
collection row may not exist yet — pre-create it (and the default catalog)
mirroring services/bootstrap.py; bootstrap is get-or-create and adopts the row.

Known ceiling: if the public collection row is ever torn down and re-minted
with a new UUID, the index predicate goes stale and uniqueness silently
re-applies to the new public collection — fail-closed (409s return, no
corruption). Recovery = recreate the index with the new UUID (exactly what
tests/conftest.py's db_clean does after each TRUNCATE). A fixed well-known
UUID can't be retrofitted: prod/review rows already exist with FKs.

downgrade() restores the full table constraint — it FAILS once public
duplicates exist (effectively one-way, same posture as 0010).

Identity/dedup SQL untouched — NO ``dedup_recipe_stamp`` row.

Revision ID: 0012_public_external_id
Revises: 0011_rename_writable_anon
Create Date: 2026-07-16
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_public_external_id"
down_revision: str | None = "0011_rename_writable_anon"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors config.Settings.public_collection / catalog_repo.DEFAULT_CATALOG_SLUG —
# migrations build DatabaseSettings only, so the slug comes straight from the env.
_PUBLIC_SLUG = os.environ.get("GEOID_PUBLIC_COLLECTION", "public")
_DEFAULT_CATALOG_SLUG = "geoid"


def _public_collection_id(bind) -> uuid.UUID:
    found = bind.execute(
        sa.text("SELECT id FROM collection WHERE slug = :slug"), {"slug": _PUBLIC_SLUG}
    ).scalar()
    if found is not None:
        return found

    catalog_id = bind.execute(
        sa.text("SELECT id FROM catalog WHERE slug = :slug"), {"slug": _DEFAULT_CATALOG_SLUG}
    ).scalar()
    if catalog_id is None:
        catalog_id = bind.execute(
            sa.text(
                "INSERT INTO catalog (id, slug, title) "
                "VALUES (gen_random_uuid(), :slug, 'GeoID') RETURNING id"
            ),
            {"slug": _DEFAULT_CATALOG_SLUG},
        ).scalar()
    return bind.execute(
        sa.text(
            "INSERT INTO collection (id, catalog_id, slug, title, public_write) "
            "VALUES (gen_random_uuid(), :catalog_id, :slug, "
            "'Public (anonymous contributions)', true) RETURNING id"
        ),
        {"catalog_id": catalog_id, "slug": _PUBLIC_SLUG},
    ).scalar()


def upgrade() -> None:
    public_id = _public_collection_id(op.get_bind())
    op.drop_constraint("uq_place_collection_external_id", "place")
    op.execute(
        "CREATE UNIQUE INDEX uq_place_collection_external_id "
        "ON place (collection_id, external_id) "
        f"WHERE collection_id <> '{public_id}'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_place_collection_external_id")
    op.execute(
        "ALTER TABLE place ADD CONSTRAINT uq_place_collection_external_id "
        "UNIQUE (collection_id, external_id)"
    )

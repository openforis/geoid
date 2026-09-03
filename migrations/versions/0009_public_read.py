"""public_read flag on collection + creator-provenance index (private collections, Core)

Two additions from the 2026-07-03 auth/authz requirements, one migration:

- ``collection.public_read`` (boolean NOT NULL DEFAULT true): a non-public
  collection's features are 404-masked on the resolvers for callers without a
  grant (viewer+ suffices — reads sit below writes on the ladder), and the
  dedup-409 withholds the incumbent's identifiers from callers who may not read
  it. DEFAULT true preserves every shipped behavior: all existing collections
  (including the seeded ``public``) stay publicly resolvable. Metadata-only
  ALTER on PostgreSQL — no table rewrite.

- ``place_provenance_created_by_idx`` on ``(provenance->>'created_by',
  created_at)``: backs ``GET /me/geoids`` (equality filter + ORDER BY in one
  btree). Partial (``IS NOT NULL``) so anonymous mints add no index rows.
  Plain (non-CONCURRENT) DDL per the 0007 precedent: tables are small and this
  runs in the blocking migrate job.

NO ``dedup_recipe_stamp`` row: identity/dedup SQL is untouched.

Revision ID: 0009_public_read
Revises: 0008_identity_recipe_v2
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_public_read"
down_revision: str | None = "0008_identity_recipe_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE collection ADD COLUMN public_read boolean NOT NULL DEFAULT true;")
    op.execute(
        "CREATE INDEX place_provenance_created_by_idx "
        "ON place ((provenance->>'created_by'), created_at) "
        "WHERE provenance->>'created_by' IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS place_provenance_created_by_idx;")
    op.execute("ALTER TABLE collection DROP COLUMN public_read;")

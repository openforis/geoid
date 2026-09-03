"""composite paging index (collection_id, created_at, id) for the OGC items listing

Backs `WHERE collection_id = :c ORDER BY created_at, id LIMIT :n OFFSET :o`, turning
the per-request plan from a full collection scan + sort into a bounded index range
scan, and makes the unfiltered `count(*)` per collection an index-only scan.

Revision ID: 0002_paging_index
Revises: 0001_initial
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_paging_index"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain CREATE INDEX is fine on a fresh DB. For an already-populated table,
    # build it CONCURRENTLY outside a transaction to avoid locking writes.
    op.create_index(
        "place_collection_created_id_idx",
        "place",
        ["collection_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("place_collection_created_id_idx", table_name="place")

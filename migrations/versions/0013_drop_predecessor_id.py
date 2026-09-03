"""Drop place.predecessor_id (supersession removed entirely)

The predecessor/supersession feature was removed from the API on 2026-07-16
(read surface included): the concept has no OGC basis — absent from OGC API
Features Part 1 and the Part 4 draft; ``predecessor-version`` is RFC 5829 via
an optional Candidate-maturity STAC extension only. The write path never
existed, so every row's value is NULL — the drop is data-lossless
(metadata-only DDL, instantaneous) and the downgrade genuinely restores the
old schema. The unnamed inline FK (``place_predecessor_id_fkey``) drops with
the column; BEFORE UPDATE/DELETE triggers don't fire on DDL, so no disable
dance. ``place_block_mutation()`` is re-created via CREATE OR REPLACE — never
DROP, its two triggers (place_block_mutation_bud, place_block_truncate_bt)
must survive — only to drop the stale ``via predecessor_id`` wording from the
RAISE message. Identity/dedup SQL untouched — NO ``dedup_recipe_stamp`` row.

Revision ID: 0013_drop_predecessor_id
Revises: 0012_public_external_id
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_drop_predecessor_id"
down_revision: str | None = "0012_public_external_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE place DROP COLUMN predecessor_id;")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION place_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $func$
        BEGIN
            RAISE EXCEPTION
                'place is immutable: % is not allowed (corrections mint a new geoid)',
                TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $func$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE place ADD COLUMN predecessor_id uuid REFERENCES place(id);")
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

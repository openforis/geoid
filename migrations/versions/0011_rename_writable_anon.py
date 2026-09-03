"""Rename collection.writable_anon to public_write (API-name parity)

The API field and ORM attribute became ``public_write`` on 2026-07-09; the
column kept its legacy name only to avoid claiming migration number 0010 ahead
of the unmerged async-import branch. 0010 is now taken (prune_submitted_
properties), so the deferral buys nothing — rename the column and drop the
ORM name override. Metadata-only DDL on PostgreSQL: instantaneous, no table
rewrite; ``collection`` carries no append-only trigger, so no disable dance.

Deploy window: between migrate and service deploy the OLD revision still
selects ``writable_anon`` and errors on collection-touching requests (~2 min,
same accepted class as 0010's window). Identity/dedup SQL untouched — NO
``dedup_recipe_stamp`` row.

Revision ID: 0011_rename_writable_anon
Revises: 0010_prune_submitted_properties
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_rename_writable_anon"
down_revision: str | None = "0010_prune_submitted_properties"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE collection RENAME COLUMN writable_anon TO public_write;")


def downgrade() -> None:
    op.execute("ALTER TABLE collection RENAME COLUMN public_write TO writable_anon;")

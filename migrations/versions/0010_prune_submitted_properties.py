"""Strip submitted_properties/client from stored provenance (geoid-prov/0.2)

Policy change (2026-07-16): GeoJSON Feature ``properties`` are accepted at
ingestion (RFC 7946) but never persisted. Provenance becomes exactly
``{schema, created_by, originating_instance}``. This migration erases the
already-stored ``submitted_properties`` and ``client`` blocks and restamps the
schema tag. The geoid/hash recipe reads only ``type`` + ``coordinates`` —
identity is untouched, so NO ``dedup_recipe_stamp`` row.

``place`` is INSERT-only (``place_block_mutation_bud``, 0001). The named
DISABLE TRIGGER needs only table ownership (not superuser, unlike
``session_replication_role``) and is transactional DDL — all three statements
run in alembic's single transaction, so the immutability guard can never be
left off. The UPDATE's WHERE clause makes it idempotent (no-op on fresh DBs).

Deploy window: between migrate and service deploy the old revision can still
mint a few 0.1-shaped rows. The UPDATE is idempotent — re-run the three
statements manually to sweep stragglers if it matters.

``downgrade()`` raises: the erased properties are unrecoverable by design.

Revision ID: 0010_prune_submitted_properties
Revises: 0009_public_read
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_prune_submitted_properties"
down_revision: str | None = "0009_public_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE place DISABLE TRIGGER place_block_mutation_bud;")
    op.execute(
        """
        UPDATE place
        SET provenance = (provenance - 'submitted_properties' - 'client')
            || jsonb_build_object('schema', 'geoid-prov/0.2')
        WHERE provenance ? 'submitted_properties'
           OR provenance ? 'client'
           OR provenance->>'schema' IS DISTINCT FROM 'geoid-prov/0.2';
        """
    )
    op.execute("ALTER TABLE place ENABLE TRIGGER place_block_mutation_bud;")


def downgrade() -> None:
    raise RuntimeError(
        "0010_prune_submitted_properties is irreversible: the erased "
        "submitted_properties/client blocks cannot be reconstructed."
    )

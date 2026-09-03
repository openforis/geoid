"""per-collection grants (custom RBAC beside Keycloak authN)

Adds ``collection_grant``: per-collection ``owner``/``editor``/``viewer`` grants
keyed by (verified) email, with the Keycloak ``sub`` backfilled on first authorized
access. Unlike ``place`` / ``geoid_registry`` / ``change_log`` this table is
**MUTABLE** (grants are upserted/revoked) — deliberately NO append-only triggers.

Scope is Core auth only: app-level (sysadmin) and per-collection write/manage authZ.
Private collections (``collection.public_read`` + read masking) and self-service
collection lifecycle are a deferred phase, so this migration adds NO ``collection``
columns — the table is purely additive and the existing read/write paths are
unchanged for anonymous and admin callers.

Revision ID: 0006_grants
Revises: 0005_support_point_geometries
Create Date: 2026-06-26

(The revision id is kept ≤32 chars — Alembic's alembic_version.version_num is
varchar(32) — so it is the short ``0006_grants``, not the full filename.)
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_grants"
down_revision: str | None = "0005_support_point_geometries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # collection_grant: per-collection RBAC (MUTABLE — no append-only triggers).
    op.execute(
        """
        CREATE TABLE collection_grant (
            id                 uuid PRIMARY KEY,
            collection_id      uuid NOT NULL,
            principal_type     text NOT NULL DEFAULT 'user',
            principal_email    text NOT NULL,
            principal_subject  text,
            role               text NOT NULL,
            granted_by         text,
            created_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_collection_grant_collection
                FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
            CONSTRAINT ck_collection_grant_role
                CHECK (role IN ('owner', 'editor', 'viewer')),
            CONSTRAINT ck_collection_grant_principal_type
                CHECK (principal_type IN ('user', 'group')),
            CONSTRAINT uq_collection_grant_principal
                UNIQUE (collection_id, principal_type, principal_email)
        );
        """
    )
    op.execute(
        "CREATE INDEX collection_grant_principal_email_idx ON collection_grant (principal_email);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS collection_grant_principal_email_idx;")
    op.execute("DROP TABLE IF EXISTS collection_grant;")

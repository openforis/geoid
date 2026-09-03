"""Stubbed tables (HINGE seams, not yet implemented).

``authority_assertion`` — a claim of authority *over* a place, append-only by
design (not yet trigger-enforced), never a mutable column *on* the place. This is
the architectural answer to "anonymous yet authority-tracked"; the seam is still
load-bearing in the provenance design.

``api_key`` — reserved for a machine-to-machine key surface. Its original
rationale expired when auth went Keycloak-only (2026-07-08); it stays pending an
explicit keep-or-drop decision (dropping it is a migration).

Both tables exist (so links and queries are forward-compatible) but carry no
behaviour yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class AuthorityAssertion(Base):
    __tablename__ = "authority_assertion"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    geoid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    asserted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_type: Mapped[str | None] = mapped_column(String, nullable=True)
    claim: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ApiKey(Base):
    __tablename__ = "api_key"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    key_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    principal: Mapped[str | None] = mapped_column(String, nullable=True)
    scopes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

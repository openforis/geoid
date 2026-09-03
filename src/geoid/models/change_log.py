"""Change log (HINGE 2) — append-only audit today, federation pull-feed later.

A monotonic ``seq`` cursor over every mint. Audit value now; later central pulls
``GET /changes?since=<seq>`` (ndjson) and does idempotent
``UPSERT ON CONFLICT (geoid) DO NOTHING`` + provenance append. Global geoid
uniqueness + immutable geoid-keyed records make the merge a conflict-free union.
Populated by the ``place`` ``AFTER INSERT`` trigger (the registry, by contrast,
is written by the app's arbiter CTE before the place insert).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class ChangeLog(Base):
    __tablename__ = "change_log"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    geoid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(String, nullable=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

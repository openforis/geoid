"""Place ORM model (≈ STAC item) — INSERT-only; ``id`` IS the geoid (content-addressed UUIDv8).

Immutability is enforced in the database (a ``BEFORE UPDATE OR DELETE`` trigger
raises), not in Python. The dedup ``geom_hash`` and its global UNIQUE live on
``geoid_registry`` (sharding-ready), not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class Place(Base):
    __tablename__ = "place"
    __table_args__ = (
        # external_id uniqueness is a partial UNIQUE INDEX of the same name
        # (uq_place_collection_external_id) excluding the public collection's
        # runtime UUID — inexpressible here, DB-only since migration 0012.
        CheckConstraint(
            "GeometryType(geom) IN ('POINT', 'MULTIPOINT', 'POLYGON', 'MULTIPOLYGON')",
            name="ck_place_geom_is_supported",
        ),
        CheckConstraint("NOT ST_IsEmpty(geom)", name="ck_place_geom_not_empty"),
        CheckConstraint("ST_IsValid(geom)", name="ck_place_geom_is_valid"),
    )

    # id == geoid: a content-addressed UUIDv8 derived DB-side from the geometry by
    # geoid_id_default (migration 0004), not minted app-side.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection.id"), nullable=False
    )
    geom: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    originating_instance: Mapped[str | None] = mapped_column(String, nullable=True)

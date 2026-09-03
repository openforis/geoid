"""Geoid registry (HINGE 1) — BOTH global uniqueness invariants on one table.

Global geoid uniqueness (PK) AND the global geometry-dedup UNIQUE
(``uq_geoid_registry_geom_hash`` on ``geom_hash``) live HERE, not as ``UNIQUE``
constraints on ``place``. That separation is what lets ``place`` be partitioned by
``collection_id`` and later Citus-sharded (Citus requires unique keys to include
the distribution column; a global ``UNIQUE(geoid)`` or ``UNIQUE(geom_hash)`` on a
sharded ``place`` would be impossible, but a reference-table registry stays
globally unique). Populated by the app's arbiter CTE (``place_repo.insert_place``),
not a trigger — the registry insert is the dedup arbiter and must run before the
``place`` insert.
"""

from __future__ import annotations

import uuid

from sqlalchemy import LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from geoid.db import Base


class GeoidRegistry(Base):
    __tablename__ = "geoid_registry"
    __table_args__ = (UniqueConstraint("geom_hash", name="uq_geoid_registry_geom_hash"),)

    geoid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Invariant: place_id == geoid (place.id IS the geoid; the arbiter CTE writes
    # the same derived value into both). Kept as its own column because the
    # registry is the sharding hinge and must stand alone as a reference table.
    place_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Deliberately denormalized from place.collection_id: the idempotent arbiter's
    # consistency lookup reads THIS copy, so it keeps working when place is sharded.
    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The global geometry-dedup hash (canonical recipe @ the one global grid).
    # Written by the arbiter CTE; the UNIQUE above is the enforced dedup invariant.
    geom_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

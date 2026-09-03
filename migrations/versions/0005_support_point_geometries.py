"""accept Point/MultiPoint: widen the geom-type CHECK + add a not-empty guard

The recipe (``geoid_geom_hash_default``), the UUIDv8 derivation, and the whole
read/write path are already geometry-type-agnostic; Point/MultiPolygon-only was
enforced only by the ``ck_place_geom_is_polygonal`` CHECK. This migration relaxes
that gate to also admit ``POINT``/``MULTIPOINT`` (lines and GeometryCollection stay
rejected) and adds ``ck_place_geom_not_empty`` so an empty geometry — e.g. an empty
MultiPoint, which would hash to a constant per-type WKB and falsely dedup unrelated
rows onto one geoid — can never be stored.

NO ``dedup_recipe_stamp`` row: the recipe SQL, the ``1e-7`` grid, and the GEOS stack
are unchanged — this widens which geometry *types* the gate admits, not the identity
recipe. A point dedups to the same geoid as a polygon vertex under the same frozen v1
recipe (ADR-005). Adding a stamp would falsely imply an identity event.

ADD validates existing rows: all are real non-empty polygons, so both new CHECKs
pass. ``ck_place_geom_not_empty`` also retroactively closes the same empty-geometry
hole for polygons (harmless).

DOWNGRADE IS EFFECTIVELY ONE-WAY once Point/MultiPoint data exists: ``downgrade()``
re-adds the polygonal-only CHECK, which VALIDATES existing rows and aborts
(non-destructively) on the first stored point. Accepted: un-accepting stored data
would be a data-loss decision, not a schema one, so there is deliberately no
``NOT VALID`` escape hatch here.

Revision ID: 0005_support_point_geometries
Revises: 0004_deterministic_geoid
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_support_point_geometries"
down_revision: str | None = "0004_deterministic_geoid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE place DROP CONSTRAINT ck_place_geom_is_polygonal;")
    op.execute(
        "ALTER TABLE place ADD CONSTRAINT ck_place_geom_is_supported "
        "CHECK (GeometryType(geom) IN ('POINT','MULTIPOINT','POLYGON','MULTIPOLYGON'));"
    )
    op.execute(
        "ALTER TABLE place ADD CONSTRAINT ck_place_geom_not_empty CHECK (NOT ST_IsEmpty(geom));"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE place DROP CONSTRAINT ck_place_geom_not_empty;")
    op.execute("ALTER TABLE place DROP CONSTRAINT ck_place_geom_is_supported;")
    op.execute(
        "ALTER TABLE place ADD CONSTRAINT ck_place_geom_is_polygonal "
        "CHECK (GeometryType(geom) IN ('POLYGON','MULTIPOLYGON'));"
    )

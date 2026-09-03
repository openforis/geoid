"""SQLAlchemy ORM models.

The schema is owned by Alembic migrations (they carry triggers and functions that
autogenerate cannot express); these models mirror it with two distinct roles:
``Catalog``/``Collection``/``CollectionGrant`` are the live runtime ORM surface
(collection + grant reads, authz, bootstrap), while ``Place``/``GeoidRegistry``/
``ChangeLog`` are schema mirrors with no runtime query path — production reads go
through raw SQL in ``place_repo``, and the write hot path (place insert + dedup)
is the hand-written arbiter CTE, which computes ``geom_hash`` in-statement (no
trigger computes it) and exercises ``ON CONFLICT`` directly.

Constraint names here MUST match the migration — ``api/errors.py`` switches on
them to map SQLSTATE 23505 to the right HTTP status.
"""

from geoid.models.catalog import Catalog
from geoid.models.change_log import ChangeLog
from geoid.models.collection import Collection
from geoid.models.collection_grant import CollectionGrant
from geoid.models.geoid_registry import GeoidRegistry
from geoid.models.place import Place
from geoid.models.stubs import ApiKey, AuthorityAssertion

__all__ = [
    "Catalog",
    "Collection",
    "CollectionGrant",
    "Place",
    "GeoidRegistry",
    "ChangeLog",
    "AuthorityAssertion",
    "ApiKey",
]

# Constraint names referenced by error mapping (single source of truth).
UQ_COLLECTION_CATALOG_SLUG = "uq_collection_catalog_slug"
# Since 0012 a partial UNIQUE INDEX (public collection excluded), not a table
# constraint — Postgres still reports its violations under this name.
UQ_PLACE_EXTERNAL_ID = "uq_place_collection_external_id"
# The global geometry-dedup UNIQUE lives on geoid_registry (sharding-ready), not place.
UQ_GEOID_REGISTRY_GEOM_HASH = "uq_geoid_registry_geom_hash"
PK_GEOID_REGISTRY = "geoid_registry_pkey"
PK_PLACE = "place_pkey"
# Per-collection grant constraints (migration 0006; collection_grant is mutable).
UQ_COLLECTION_GRANT_PRINCIPAL = "uq_collection_grant_principal"
CK_COLLECTION_GRANT_ROLE = "ck_collection_grant_role"
CK_COLLECTION_GRANT_PRINCIPAL_TYPE = "ck_collection_grant_principal_type"
FK_COLLECTION_GRANT_COLLECTION = "fk_collection_grant_collection"
# Hardening CHECK (migration 0007). Surfaces as SQLSTATE 23514; named here for
# tests/diagnostics. (2D-only needs no CHECK: the place.geom column typmod
# geometry(Geometry, 4326) already rejects Z/M at the type level.)
CK_COLLECTION_GRANT_EMAIL_NORMALIZED = "ck_collection_grant_email_normalized"

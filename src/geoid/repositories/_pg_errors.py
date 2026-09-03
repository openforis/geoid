"""PostgreSQL error introspection — the SQLSTATE/constraint-name seam.

Lives in the repository layer (the SQL boundary) so BOTH the API error mapper
(``api/errors.py``) and the registry service (``services/registry_service.py``)
can import it without ``services`` reaching back into ``api`` — the import
inversion the per-feature bulk classification would otherwise create.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, IntegrityError

# PostgreSQL SQLSTATEs the write path discriminates on.
SQLSTATE_CHECK_VIOLATION = "23514"  # invalid / unsupported-type / empty geometry (DB CHECK)
SQLSTATE_RESTRICT_VIOLATION = "23001"  # raised by the immutability trigger
SQLSTATE_CHARACTER_NOT_IN_REPERTOIRE = "22021"  # NUL byte in an input string (path or body)

# Custom SQLSTATE raised by the v2 identity recipe (migration 0008) when a VALID
# geometry degenerates on the 1e-7 lattice (ring < 3 distinct vertices, or exact-
# zero integer shoelace). Deliberately NOT 23514: that path recovers the reason
# via ST_IsValidReason, which answers "Valid Geometry" for a lattice-degenerate
# sliver. Class GD is outside SQLSTATE class 23, so SQLAlchemy wraps it as a
# generic DBAPIError (not IntegrityError) — handled in registry_service's
# DBAPIError branches. The schema layer pre-rejects these with a clean 422; this
# is the DB backstop for writes that bypass the schema.
SQLSTATE_DEGENERATE_GEOMETRY = "GD001"

# ST_GeomFromGeoJSON parse failures for malformed GeoJSON text: PostGIS lwgeom
# errors surface as XX000 (internal_error), bad parameters as 22023
# (invalid_parameter_value). The write path maps ONLY these DBAPIError states to
# the client-facing 422/invalid_geometry; any other sqlstate (e.g. 42883, a
# missing SQL function — the June-15 outage signature) is a server-side failure
# and must stay loud, never be mislabelled as the client's geometry.
GEOJSON_PARSE_SQLSTATES = frozenset({"XX000", "22023"})


def sqlstate_of(exc: DBAPIError) -> str | None:
    """The SQLSTATE on a SQLAlchemy asyncpg error (``exc.orig.sqlstate``)."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def pg_fields(exc: IntegrityError) -> tuple[str | None, str | None]:
    """Extract ``(constraint_name, sqlstate)`` from a SQLAlchemy asyncpg IntegrityError.

    SQLAlchemy's asyncpg adapter exposes ``sqlstate`` on ``exc.orig`` but the
    ``constraint_name`` only on the wrapped asyncpg error at ``exc.orig.__cause__``.
    """
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    return constraint, sqlstate

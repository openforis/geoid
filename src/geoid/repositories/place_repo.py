"""Place data access — the arbiter-CTE insert and the OGC read queries.

The insert is the product's hot path. ``geoid_registry`` is the global
geometry-dedup arbiter: a single-statement CTE inserts the registry row
(``geom_hash`` via ``geoid_geom_hash_default``) with ``ON CONFLICT ... DO
NOTHING`` on ``uq_geoid_registry_geom_hash``, then inserts the ``place`` row ONLY
if the registry arbiter won. So an identical-geometry clash (catalog-wide) writes
no row and never aborts the transaction, while an external_id or geoid clash on
the ``place`` leg still raises 23505 (mapped to 409 by constraint name) and rolls
the whole statement — including the registry row — back. When the place row is
not written the geoid is still derived in-statement from the geometry (race-free)
and the mint is idempotent; only the incumbent's collection slug is read back from
``geoid_registry`` (via the same ``geoid_geom_hash_default`` wrapper, with a
bounded retry for the rare concurrent pre-commit window), as the service's proof
that the returned geoid actually resolves.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Geometry built identically everywhere: GeoJSON -> geometry, SRID pinned to 4326.
_GEOM_EXPR = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)"

# Concurrent-loser incumbent recovery: on the rare pre-commit miss (the in-statement
# LEFT JOIN ran before the winner committed) re-snapshot under READ COMMITTED a few
# times. ~60ms worst case, paid ONLY on that miss; the geoid itself never waits.
_INCUMBENT_SLUG_RETRIES = 3
_INCUMBENT_SLUG_BACKOFF_S = 0.02


@dataclass(frozen=True)
class InsertResult:
    geoid: uuid.UUID
    created: bool  # True = newly minted; False = an identical geometry already exists
    # The incumbent's collection when created=False (None on the winner path). It
    # backs ONLY the service's registry-drift guard: a resolvable incumbent must
    # exist before the idempotent mint returns its geoid.
    collection_slug: str | None = None


_SUPPORTED_GEOM_TYPES = ("POINT", "MULTIPOINT", "POLYGON", "MULTIPOLYGON")


async def geometry_invalid_reason(session: AsyncSession, geojson: str) -> str:
    """Human-readable reason a geometry failed the DB CHECK — ERROR PATH ONLY.

    Called only after an insert raised a 23514 check violation, so the geometry
    already parsed; this recovers ST_IsValidReason (and flags an unsupported type —
    a line or GeometryCollection) for the 422 body. Keeping it off the happy path
    is the write-path optimization.
    """
    stmt = text(
        f"SELECT GeometryType(g) AS t, ST_IsValidReason(g) AS reason "
        f"FROM (SELECT {_GEOM_EXPR} AS g) s"
    )
    row = (await session.execute(stmt, {"geojson": geojson})).mappings().first()
    if row is None:
        return "invalid geometry"
    if row["t"] not in _SUPPORTED_GEOM_TYPES:
        return f"unsupported geometry type: {row['t']}"
    return row["reason"] or "invalid geometry"


async def _resolve_incumbent(session: AsyncSession, geojson: str) -> str | None:
    """Re-read the incumbent's collection slug until the winner's row is visible.

    Paid ONLY on the rare concurrent pre-commit miss, where the in-statement LEFT
    JOIN couldn't yet see the winner's not-yet-committed registry row. Each execute
    starts a fresh statement snapshot under READ COMMITTED, so a bounded retry
    catches the winner's commit; the geoid is already derived in-statement and never
    depends on this. Without the retry that window would raise spurious 500s from
    the service's drift guard. Returns None if the row never appears (genuine
    recipe drift).
    """
    stmt = text(
        "SELECT c.slug "
        "FROM geoid_registry r JOIN collection c ON c.id = r.collection_id "
        f"WHERE r.geom_hash = geoid_geom_hash_default({_GEOM_EXPR})"
    )
    for _ in range(_INCUMBENT_SLUG_RETRIES):
        row = (await session.execute(stmt, {"geojson": geojson})).first()
        if row is not None:
            return str(row[0])
        await asyncio.sleep(_INCUMBENT_SLUG_BACKOFF_S)
    return None


async def insert_place(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    geojson: str,
    external_id: str | None,
    provenance: dict[str, Any],
    originating_instance: str | None,
) -> InsertResult:
    """Insert a place; an identical geometry ANYWHERE in the catalog dedups.

    Single-statement arbiter CTE. The geoid is **derived DB-side** from the geometry:
    the ``calc``/``ids`` legs compute the canonical ``geom_hash`` once and the
    deterministic geoid ``geoid_from_geom_hash(h)`` from it, so identity and dedup
    come from one fingerprint and cannot drift (migration 0004). The ``arb`` leg
    inserts into ``geoid_registry`` (the global dedup arbiter) ``ON CONFLICT ... DO
    NOTHING`` on the geom_hash UNIQUE, and the ``place`` leg inserts only for the
    geoid the arbiter accepted (``JOIN arb``) — so the place row is written ONLY if
    the registry arbiter won. Both inserts are one statement / one transaction, so an
    external_id or CHECK failure on the place leg rolls the registry row back too (no
    orphan), and the AFTER INSERT trigger then appends change_log. A duplicate
    geometry returns no row (DO NOTHING, not a raised exception) — the hot dedup path
    never aborts the transaction.

    Race safety: the geoid is derived in the SAME statement as the arbiter
    (``i.geoid`` from the ``ids`` leg), so a concurrent loser ALWAYS gets the real
    incumbent geoid with no cross-statement visibility gap — never a geoid-less
    response. The final SELECT prefers the STORED registry geoid
    (``COALESCE(r.geoid, i.geoid)``): identical to ``i.geoid`` on the winner path
    (the LEFT JOIN can't see the arbiter's own insert, so ``r`` is NULL) and on
    every post-0004 loser, but self-correcting against a legacy/drifted row whose
    stored geoid predates deterministic derivation — the returned geoid must be one
    that actually resolves. Only the incumbent's collection can be missing, in the narrow
    concurrent pre-commit window where the in-statement ``LEFT JOIN`` ran before the
    winner committed; ``_resolve_incumbent`` re-snapshots under READ COMMITTED
    with a bounded retry to recover it. If it still never appears (genuine recipe
    drift) the result carries ``collection_slug=None`` and the service raises
    ``RegistryConsistencyError`` (a structured 500) — the repo reports the fact, it
    does not decide the HTTP outcome. This retry relies on READ COMMITTED taking a
    fresh snapshot per statement; it breaks under REPEATABLE READ.

    Dual-violation precedence: when a submission duplicates BOTH the geometry and
    an existing external_id, the geometry arbiter wins — the registry leg runs
    first and DOES NOTHING, so the place leg inserts no row and its external_id
    index is never touched; the request yields the idempotent geometry result
    (the incumbent geoid, 201) rather than the external_id 409. Pinned by
    ``test_review_fixes.py::test_dual_geometry_and_external_id_duplicate_yields_incumbent_geoid``.
    """
    insert_stmt = text(
        f"""
        WITH calc AS (
            SELECT g, geoid_geom_hash_default(g) AS h
            FROM (SELECT {_GEOM_EXPR} AS g) src
        ),
        ids AS (
            SELECT g, h, geoid_from_geom_hash(h) AS geoid FROM calc
        ),
        arb AS (
            INSERT INTO geoid_registry (geoid, place_id, collection_id, geom_hash)
            SELECT geoid, geoid, :collection_id, h FROM ids
            ON CONFLICT ON CONSTRAINT uq_geoid_registry_geom_hash DO NOTHING
            RETURNING geoid
        ),
        ins AS (
            INSERT INTO place (
                id, collection_id, geom, external_id, provenance,
                originating_instance
            )
            SELECT
                i.geoid, :collection_id, i.g, :external_id,
                CAST(:provenance AS jsonb), :originating_instance
            FROM ids i
            JOIN arb a ON a.geoid = i.geoid
            RETURNING id
        )
        SELECT
            COALESCE(r.geoid, i.geoid) AS geoid,
            EXISTS (SELECT 1 FROM ins) AS created,
            c.slug AS incumbent_slug
        FROM ids i
        LEFT JOIN geoid_registry r ON r.geom_hash = i.h
        LEFT JOIN collection c ON c.id = r.collection_id
        """
    )
    params = {
        "collection_id": collection_id,
        "geojson": geojson,
        "external_id": external_id,
        "provenance": json.dumps(provenance),
        "originating_instance": originating_instance,
    }
    row = (await session.execute(insert_stmt, params)).first()
    geoid, created = row[0], row[1]
    if created:
        return InsertResult(geoid=geoid, created=True)

    # Dedup loser: the geoid above is the deterministic incumbent geoid (derived
    # in-statement, never null). Only the incumbent's collection can lag — NULL in
    # the rare concurrent pre-commit window where the LEFT JOIN ran before the
    # winner committed — so re-read it with a bounded retry. If it still never
    # appears (genuine recipe drift) report the fact with collection_slug=None; the
    # service decides the HTTP outcome (RegistryConsistencyError → structured 500).
    slug = row[2] if row[2] is not None else await _resolve_incumbent(session, geojson)
    return InsertResult(geoid=geoid, created=False, collection_slug=slug)


# --- Read path --------------------------------------------------------------

_READ_COLUMNS = """
    p.id AS geoid,
    c.slug AS collection_slug,
    c.id AS collection_id,
    ST_AsGeoJSON(p.geom) AS geometry,
    p.external_id,
    p.provenance,
    p.created_at,
    p.originating_instance
"""

# Listings are geometry-free (PlaceRecord): never pay ST_AsGeoJSON or ship
# provenance for rows whose geometry the response discards.
_LIST_COLUMNS = """
    p.id AS geoid,
    c.slug AS collection_slug,
    p.external_id,
    p.created_at
"""


async def get_by_geoid(session: AsyncSession, geoid: uuid.UUID) -> dict[str, Any] | None:
    stmt = text(
        f"""
        SELECT {_READ_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.id = :geoid
        """
    )
    row = (await session.execute(stmt, {"geoid": geoid})).mappings().first()
    return dict(row) if row else None


async def get_by_external_id(
    session: AsyncSession, collection_id: uuid.UUID, external_id: str
) -> dict[str, Any] | None:
    stmt = text(
        f"""
        SELECT {_READ_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.collection_id = :collection_id AND p.external_id = :external_id
        """
    )
    row = (
        (await session.execute(stmt, {"collection_id": collection_id, "external_id": external_id}))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def list_by_creator(
    session: AsyncSession, created_by: str, *, limit: int, offset: int
) -> list[dict[str, Any]]:
    """The caller's own mints, newest first (backs ``GET /me/geoids``).

    Filter + ORDER BY ride ``place_provenance_created_by_idx`` (migration 0009);
    ``id`` tiebreaks equal timestamps for a stable page order.
    """
    stmt = text(
        f"""
        SELECT {_LIST_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.provenance->>'created_by' = :created_by
        ORDER BY p.created_at DESC, p.id
        LIMIT :limit OFFSET :offset
        """
    )
    rows = (
        (await session.execute(stmt, {"created_by": created_by, "limit": limit, "offset": offset}))
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def list_by_collection(
    session: AsyncSession, collection_id: uuid.UUID, *, limit: int, offset: int
) -> list[dict[str, Any]]:
    """A collection's items, newest first (the sysadmin-only /manage inventory).

    # ponytail: unindexed sort — 0007 dropped the (collection_id, created_at, id)
    # paging index as write amplification; this admin-only inventory tolerates the
    # seq-scan sort. Re-add that index if the listing ever gets hot.
    """
    stmt = text(
        f"""
        SELECT {_LIST_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.collection_id = :collection_id
        ORDER BY p.created_at DESC, p.id
        LIMIT :limit OFFSET :offset
        """
    )
    rows = (
        (
            await session.execute(
                stmt, {"collection_id": collection_id, "limit": limit, "offset": offset}
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def collection_extent(
    session: AsyncSession, collection_id: uuid.UUID
) -> tuple[float, float, float, float] | None:
    """The real CRS84 bbox over a collection's geometries, or None when empty."""
    stmt = text(
        """
        SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (SELECT ST_Extent(geom) AS e FROM place WHERE collection_id = :c) s
        """
    )
    row = (await session.execute(stmt, {"c": collection_id})).first()
    if row is None or row[0] is None:
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


async def collection_extents(
    session: AsyncSession, collection_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, float, float, float]]:
    """Batch real extents for many collections in ONE query (avoids the N+1 on /collections)."""
    if not collection_ids:
        return {}
    stmt = text(
        """
        SELECT collection_id, ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (
            SELECT collection_id, ST_Extent(geom) AS e
            FROM place
            WHERE collection_id = ANY(:ids)
            GROUP BY collection_id
        ) s
        """
    )
    rows = (await session.execute(stmt, {"ids": list(collection_ids)})).all()
    out: dict[uuid.UUID, tuple[float, float, float, float]] = {}
    for r in rows:
        if r[1] is not None:
            out[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    return out

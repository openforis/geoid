"""Identity recipe v2 — the integer-lattice canonical geometry fingerprint (ADR-007).

The geoid is a content-addressed UUIDv8 derived from the geometry's canonical
SHA-256 ``geom_hash``. Recipe v1 delegated canonicalization to GEOS
(``ST_MakeValid`` → ``ST_ReducePrecision`` → ``ST_Normalize`` → WKB), which made
identity depend on the deployed GEOS build (proven drift: GEOS 3.9 ↔ 3.11 ↔ 3.13
each round sub-unit grids differently). v2 removes every engine call from the
identity bytes: quantize each coordinate onto the 1e-7° integer lattice, own the
canonical structure as frozen spec constants, and hash a canonical big-endian
serialization of the **int64 lattice indices** — never a reconstructed float, so
reverse-scale bit drift and IEEE −0.0 are impossible by construction.

The spec (frozen; a change is an identity-version event):

1. Quantization: ``q = round_half_even(coord × 10000000.0)`` — ONE IEEE-754
   double multiply by the integer ``SCALE`` = 10^7 (exactly representable as a
   float64; 1e-7 is not — multiply, never divide). |lon|·1e7 ≤ 1.8e9 → int64
   with ~5× headroom. Ties round half-even (rint semantics): Python ``round``,
   PG ``float8 → bigint`` cast — identical on the exact binary product.
2. Ring: quantize every listed vertex → remove consecutive duplicate lattice
   points cyclically (forward pass + wrap-around trim; subsumes the closing
   vertex, so no step depends on parser closure representation) → fewer than 3
   vertices left OR exact integer shoelace == 0 → **degenerate, reject** →
   orient every ring CCW (positive shoelace) → rotate the lexicographically
   smallest ``(x, y)`` vertex to front (ties: the rotation with the smallest
   flattened index sequence). Collinear vertices are NEVER removed — an
   inserted midpoint stays detectable (v1 rule preserved); only coincident
   runs collapse. Shoelace is exact integer arithmetic (a 3-term product sum
   can overflow int64 at world extent).
3. Polygon: exterior ring first, then holes sorted by canonical serialized
   bytes. MultiPolygon: polygon bodies sorted by canonical bytes. MultiPoint:
   members sorted by ``(x, y)``, duplicates kept (no silent merge).
4. Serialization (big-endian throughout; deliberately not WKB)::

       canonical_bytes = 0x02 (version) || type_tag || body
       type_tag: Point=0x01  MultiPoint=0x02  Polygon=0x03  MultiPolygon=0x04
       ring            = int4 count || count × (int8 qx || int8 qy)
       Polygon body    = int4 nrings || exterior || sorted holes
       MultiPolygon    = int4 nparts || sorted polygon bodies
       MultiPoint body = int4 n || sorted (qx, qy) pairs
       Point body      = qx || qy
       geom_hash_v2    = sha256(canonical_bytes)

5. No repair of any kind inside the hash pipeline (v1's ``ST_MakeValid`` leg is
   dropped): validity is a boundary concern (schema 422 + the DB CHECK), and a
   geometry that degenerates on the lattice is rejected — an identity service
   must not silently merge or vanish distinct submissions (v1's VALID_OUTPUT
   mode silently emptied collapsed slivers).

The database computes the authoritative value (migration 0008's
``geoid_geom_hash_v2`` — the same spec in SQL, byte-identical, pinned by the
full-corpus parity suite); this pure module is the reference for tests,
tooling, and the golden-vector generator.
"""

from __future__ import annotations

import hashlib
import struct
import uuid

from geoid.domain.identifiers import geoid_from_geom_hash

RECIPE_VERSION = "v2"

# Integer lattice scale: one cell = 1e-7° (~1cm at the equator, exact-match
# semantics). Pinned equal to migration 0008's SQL literal and to
# round(1 / settings.dedup_grid_default) by unit tests.
SCALE = 10_000_000
_SCALE_FLOAT = float(SCALE)  # 1e7 is exactly representable as a float64

_CANONICAL_VERSION = b"\x02"
_TYPE_TAGS = {
    "Point": b"\x01",
    "MultiPoint": b"\x02",
    "Polygon": b"\x03",
    "MultiPolygon": b"\x04",
}

DEGENERATE_MESSAGE = "geometry degenerates at the identity precision (1e-7°)"


class DegenerateGeometryError(ValueError):
    """A valid geometry that collapses on the identity lattice (ring < 3 distinct
    lattice vertices, or exact-zero integer shoelace area). Rejected, never
    silently merged/vanished — the v2 analogue of ADR-006's Z rejection."""


def quantize(coord: float) -> int:
    """Quantize one coordinate onto the 1e-7° lattice: half-even on the exact
    IEEE-754 product ``coord × 1e7``. Mirrors PG ``(c * 10000000.0::float8)::bigint``."""
    return round(coord * _SCALE_FLOAT)


def _quantize_position(position: object) -> tuple[int, int]:
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise ValueError(
            "position must be a 2D [lon, lat] pair (GeoID is 2D-only; "
            "Z is rejected at the schema boundary)"
        )
    return (quantize(position[0]), quantize(position[1]))


def _canon_ring(ring: list) -> list[tuple[int, int]]:
    """Canonicalize one ring to its open lattice-vertex list per the spec."""
    points = [_quantize_position(p) for p in ring]

    # Cyclic consecutive-duplicate removal: forward pass, then wrap-around trim
    # (also removes the GeoJSON closing vertex — no closure-dependent step).
    kept: list[tuple[int, int]] = []
    for point in points:
        if not kept or point != kept[-1]:
            kept.append(point)
    while len(kept) > 1 and kept[-1] == kept[0]:
        kept.pop()

    if len(kept) < 3:
        raise DegenerateGeometryError(
            f"{DEGENERATE_MESSAGE}: ring collapses to {len(kept)} distinct lattice vertices"
        )

    # Exact integer shoelace (2×signed area on the lattice) — Python ints never
    # overflow; the SQL twin accumulates in numeric for the same reason.
    shoelace = 0
    count = len(kept)
    for i, (x1, y1) in enumerate(kept):
        x2, y2 = kept[(i + 1) % count]
        shoelace += x1 * y2 - x2 * y1
    if shoelace == 0:
        raise DegenerateGeometryError(f"{DEGENERATE_MESSAGE}: ring has zero lattice area")
    if shoelace < 0:
        kept.reverse()  # orient every ring CCW; role is positional, not encoded

    # Rotate the lexicographically smallest vertex to front. If the minimum
    # occurs more than once (a lattice pinch), pick the rotation with the
    # smallest vertex sequence — fully rotation-invariant either way.
    minimum = min(kept)
    starts = [i for i, v in enumerate(kept) if v == minimum]
    if len(starts) == 1:
        i = starts[0]
        return kept[i:] + kept[:i]
    return min(kept[i:] + kept[:i] for i in starts)


def _ring_bytes(vertices: list[tuple[int, int]]) -> bytes:
    return struct.pack(">i", len(vertices)) + b"".join(
        struct.pack(">qq", x, y) for x, y in vertices
    )


def _polygon_body(rings: list) -> bytes:
    if not rings:
        raise ValueError("polygon has no rings")
    canonical = [_ring_bytes(_canon_ring(ring)) for ring in rings]
    exterior, holes = canonical[0], sorted(canonical[1:])
    return struct.pack(">i", len(canonical)) + exterior + b"".join(holes)


def canonical_bytes(geometry: dict) -> bytes:
    """Serialize a canonical GeoJSON geometry dict to its v2 canonical bytes.

    Raises :class:`DegenerateGeometryError` for a lattice-degenerate geometry
    and ``ValueError`` for an unsupported type or malformed structure.
    """
    geometry_type = geometry.get("type")
    tag = _TYPE_TAGS.get(geometry_type)
    if tag is None:
        raise ValueError(f"unsupported geometry type for identity: {geometry_type!r}")
    coordinates = geometry.get("coordinates")
    if not coordinates:
        raise ValueError("geometry is empty: it carries no coordinates")
    header = _CANONICAL_VERSION + tag

    if geometry_type == "Point":
        x, y = _quantize_position(coordinates)
        return header + struct.pack(">qq", x, y)
    if geometry_type == "MultiPoint":
        points = sorted(_quantize_position(p) for p in coordinates)
        return (
            header
            + struct.pack(">i", len(points))
            + b"".join(struct.pack(">qq", x, y) for x, y in points)
        )
    if geometry_type == "Polygon":
        return header + _polygon_body(coordinates)
    bodies = sorted(_polygon_body(rings) for rings in coordinates)
    return header + struct.pack(">i", len(bodies)) + b"".join(bodies)


def geom_hash_v2(geometry: dict) -> bytes:
    """The v2 ``geom_hash``: sha256 of the canonical bytes. Byte-identical to
    the SQL ``geoid_geom_hash_v2`` (migration 0008) — the DB value is authoritative."""
    return hashlib.sha256(canonical_bytes(geometry)).digest()


def geoid_v2(geometry: dict) -> uuid.UUID:
    """Derive the deterministic geoid from a canonical GeoJSON geometry dict."""
    return geoid_from_geom_hash(geom_hash_v2(geometry))

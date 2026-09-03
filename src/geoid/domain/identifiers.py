"""The geoid identifier: a deterministic, content-addressed UUIDv8 derived from the
geometry, plus the read-time derivations and the legacy UUIDv7 minter (now used only
for internal infrastructure ids).

The *one unrecoverable decision* in the system is that every geoid is an immutable,
**content-addressed** UUIDv8 (RFC 9562 §5.8): the first 16 bytes of the geometry's
canonical SHA-256 ``geom_hash`` with the version (8) and variant bits stamped
(:func:`geoid_from_geom_hash`). Identity is therefore a pure function of the canonical
geometry — the same geometry yields the same geoid on every deployment (federation
without coordination), and re-creating a deleted geometry recovers its geoid. The
authoritative value is computed DB-side in the arbiter CTE (migration 0004's SQL
``geoid_from_geom_hash``) from the very ``geom_hash`` used for dedup, so identity and
dedup can never drift; this Python mirror must stay byte-identical for tests/tooling.
RFC 9562 §6.5 mandates UUIDv8 (not the SHA-1 v5) for SHA-256-based name UUIDs.

:func:`uuid7` remains the monotonic UUIDv7 minter, now used only for internal
infrastructure rows (catalog/collection ids), never for geoids.

On read we derive the resolvable form from the bare UUID:

* a URI            ``<base_url>/<uuid>``
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_UUID_V7_VERSION = 0x70  # version 7 in the high nibble of byte 6
_UUID_V8_VERSION = 0x80  # version 8 in the high nibble of byte 6
_VARIANT_RFC4122 = 0x80  # variant 10xx in the high bits of byte 8

# Monotonic state (RFC 9562 §6.2 Method 1): the 12-bit rand_a field is a counter,
# randomly re-seeded on each new millisecond (random seed = cross-process spread, per
# the RFC; the trade-off is that per-ms capacity before overflow is 4096 minus the
# seed, not a fixed 4095). The lock guards the (ms, counter) snapshot; the per-mint
# rand_b urandom() runs outside it — only the once-per-ms 2-byte seed runs inside.
_COUNTER_MAX = 0xFFF  # 12 bits
_lock = threading.Lock()
_last_ms: int = 0
_counter: int = 0


def _assemble(ts_ms: int, rand_a: int, rand_b: int) -> uuid.UUID:
    """Pack the three UUIDv7 fields into the RFC 9562 §5.7 byte layout.

    Layout (128 bits): 48-bit big-endian Unix millisecond timestamp, 4-bit
    version (0b0111), 12-bit ``rand_a``, 2-bit variant (0b10), 62-bit ``rand_b``.
    The millisecond timestamp prefix makes ids k-sortable, which keeps the
    primary-key b-tree append-friendly. Inputs wider than their field are masked
    (``rand_a`` to 12 bits, ``rand_b`` to 62), so callers may pass raw urandom ints.
    """
    ts = ts_ms & ((1 << 48) - 1)
    rand_a &= 0x0FFF
    rand_b &= (1 << 62) - 1
    b = bytearray(16)

    # 48-bit timestamp, big-endian, bytes 0..5
    b[0] = (ts >> 40) & 0xFF
    b[1] = (ts >> 32) & 0xFF
    b[2] = (ts >> 24) & 0xFF
    b[3] = (ts >> 16) & 0xFF
    b[4] = (ts >> 8) & 0xFF
    b[5] = ts & 0xFF

    # version (high nibble of byte 6) + 12-bit rand_a (low nibble of 6 + byte 7)
    b[6] = _UUID_V7_VERSION | (rand_a >> 8)
    b[7] = rand_a & 0xFF

    # variant (high bits of byte 8) + 62-bit rand_b (rest of bytes 8..15)
    b[8] = _VARIANT_RFC4122 | ((rand_b >> 56) & 0x3F)
    b[9] = (rand_b >> 48) & 0xFF
    b[10] = (rand_b >> 40) & 0xFF
    b[11] = (rand_b >> 32) & 0xFF
    b[12] = (rand_b >> 24) & 0xFF
    b[13] = (rand_b >> 16) & 0xFF
    b[14] = (rand_b >> 8) & 0xFF
    b[15] = rand_b & 0xFF

    return uuid.UUID(bytes=bytes(b))


def uuid7(ts_ms: int | None = None) -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562 §5.7) with a monotonic counter (§6.2 Method 1).

    Without ``ts_ms``, ids are strictly monotonic within the process: rand_a is a
    counter incremented on same-ms mints, the timestamp is held at the last known
    ms on clock rollback (NTP, suspend/resume), and counter overflow (>4095/ms)
    advances a synthetic millisecond before re-seeding.

    Args:
        ts_ms: Override the timestamp in Unix milliseconds (tests only). Bypasses
            the monotonic state entirely and uses a random rand_a.
    """
    if ts_ms is not None:
        rand = os.urandom(10)  # 80 random bits; we consume 12 + 62 = 74 of them
        rand_a = int.from_bytes(rand[0:2], "big")
        rand_b = int.from_bytes(rand[2:10], "big")
        return _assemble(ts_ms, rand_a, rand_b)

    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            # New millisecond: advance the clock, seed the counter randomly.
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_MAX
        else:
            # Same ms OR clock rollback: hold the timestamp, increment the counter.
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Overflow: advance a synthetic millisecond, re-seed the counter.
                _last_ms += 1
                _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_MAX
        ms_snapshot = _last_ms
        counter_snapshot = _counter

    rand_b = int.from_bytes(os.urandom(8), "big")
    return _assemble(ms_snapshot, counter_snapshot, rand_b)


def geoid_from_geom_hash(geom_hash: bytes) -> uuid.UUID:
    """Derive the deterministic geoid (UUIDv8) from a geometry's SHA-256 ``geom_hash``.

    Takes the first 16 bytes of the digest and stamps the version (8) and RFC 4122
    variant bits in place, so the geoid is a pure function of the canonical geometry:
    the same geometry → the same geoid on every deployment, and a re-created geometry
    recovers its geoid. Byte-identical to the SQL ``geoid_from_geom_hash`` (migration
    0004) — the database computes the authoritative value; this mirror serves tests
    and tooling.

    Args:
        geom_hash: the canonical geometry digest (a 32-byte SHA-256; only the first
            16 bytes are consumed). Inputs shorter than 16 bytes are rejected.
    """
    if len(geom_hash) < 16:
        raise ValueError(f"geom_hash must be at least 16 bytes, got {len(geom_hash)}")
    raw = bytearray(geom_hash[:16])
    raw[6] = (raw[6] & 0x0F) | _UUID_V8_VERSION
    raw[8] = (raw[8] & 0x3F) | _VARIANT_RFC4122
    return uuid.UUID(bytes=bytes(raw))


def uri_for(value: uuid.UUID, base_url: str) -> str:
    """Derive the durable resolver URI ``<base_url>/<uuid>``."""
    return f"{base_url.rstrip('/')}/{value}"


def derive_identifiers(value: uuid.UUID, *, base_url: str) -> dict[str, str]:
    """Bundle the resolvable forms returned by POST / GET: ``geoid`` and ``uri``."""
    return {"geoid": str(value), "uri": uri_for(value, base_url)}

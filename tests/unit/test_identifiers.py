"""Unit tests for geoid identifier minting + URI derivation."""

from __future__ import annotations

import itertools
import threading
import uuid

import pytest

from geoid.domain import identifiers

pytestmark = pytest.mark.unit

_FROZEN_NS = 1_780_000_000_000_000_000  # an arbitrary fixed instant, in nanoseconds


def _embedded_ms(v: uuid.UUID) -> int:
    """Extract the UUIDv7 embedded ms timestamp (the minter's behavioral assertion)."""
    return int.from_bytes(v.bytes[0:6], "big")


def test_uuid7_is_version_7_and_rfc4122_variant():
    value = identifiers.uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_embedded_timestamp_round_trips():
    ts_ms = 1_780_000_000_000
    value = identifiers.uuid7(ts_ms=ts_ms)
    assert _embedded_ms(value) == ts_ms


def test_uuid7_is_time_ordered_across_increasing_timestamps():
    earlier = identifiers.uuid7(ts_ms=1_000_000_000_000)
    later = identifiers.uuid7(ts_ms=2_000_000_000_000)
    # k-sortable: the millisecond prefix makes string + byte order match time order.
    assert earlier.bytes < later.bytes
    assert str(earlier) < str(later)


def test_minting_is_unique_in_bulk():
    minted = {identifiers.uuid7() for _ in range(2000)}
    assert len(minted) == 2000


def test_uri_derivation_strips_trailing_slash():
    value = uuid.UUID("019e9976-974c-7d01-b2b6-299f41d9d29c")
    assert (
        identifiers.uri_for(value, "https://data.fao.org/")
        == "https://data.fao.org/019e9976-974c-7d01-b2b6-299f41d9d29c"
    )


def test_derive_identifiers_bundles_geoid_and_resolver_uri():
    value = identifiers.uuid7()
    bundle = identifiers.derive_identifiers(value, base_url="https://data.fao.org")
    assert bundle == {"geoid": str(value), "uri": f"https://data.fao.org/{value}"}


def test_same_ms_mints_are_strictly_increasing(monkeypatch):
    # Arrange: freeze the clock AT the last-seen ms so every mint takes the same-ms branch
    monkeypatch.setattr(identifiers, "_last_ms", _FROZEN_NS // 1_000_000)
    monkeypatch.setattr(identifiers, "_counter", 0)
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: _FROZEN_NS)

    # Act
    minted = [identifiers.uuid7() for _ in range(100)]

    # Assert
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
    assert all(v.version == 7 for v in minted)
    assert all(v.variant == uuid.RFC_4122 for v in minted)


def test_clock_rollback_holds_timestamp_and_stays_monotonic(monkeypatch):
    # Arrange: a clock that ticks backwards between mints (NTP adjustment)
    monkeypatch.setattr(identifiers, "_last_ms", 0)
    monkeypatch.setattr(identifiers, "_counter", 0)
    clock = {"ns": _FROZEN_NS}
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: clock["ns"])

    # Act
    minted = [identifiers.uuid7()]
    for step_back_ms in (10, 20, 30):
        clock["ns"] = _FROZEN_NS - step_back_ms * 1_000_000
        minted.append(identifiers.uuid7())

    # Assert: byte order strictly increases, embedded timestamp never decreases
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
        assert _embedded_ms(later) >= _embedded_ms(earlier)


def test_counter_overflow_advances_synthetic_ms_without_duplicates(monkeypatch):
    # Arrange: frozen clock so >4096 mints must overflow the 12-bit counter
    monkeypatch.setattr(identifiers, "_last_ms", 0)
    monkeypatch.setattr(identifiers, "_counter", 0)
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: _FROZEN_NS)

    # Act
    minted = [identifiers.uuid7() for _ in range(5000)]

    # Assert: unique, strictly increasing, and the synthetic ms actually advanced
    assert len(set(minted)) == 5000
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
    assert _embedded_ms(minted[-1]) > _FROZEN_NS // 1_000_000


def test_concurrent_minting_yields_unique_ids():
    # Arrange
    results: list[uuid.UUID] = []
    results_lock = threading.Lock()

    def mint_many() -> None:
        local = [identifiers.uuid7() for _ in range(500)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=mint_many) for _ in range(8)]

    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assert
    assert len(set(results)) == 8 * 500


def test_explicit_ts_ms_bypasses_monotonic_state(monkeypatch):
    # Arrange: pre-load recognizable monotonic state
    monkeypatch.setattr(identifiers, "_last_ms", 123)
    monkeypatch.setattr(identifiers, "_counter", 7)

    # Act
    value = identifiers.uuid7(ts_ms=1_780_000_000_000)

    # Assert: the override is honored and the shared state is untouched
    assert _embedded_ms(value) == 1_780_000_000_000
    assert identifiers._last_ms == 123
    assert identifiers._counter == 7


# --- Deterministic geoid: UUIDv8 derived from the geometry's SHA-256 geom_hash ----
# geoid_from_geom_hash MUST stay byte-identical to the SQL function of the same name
# (migration 0004) — the Python and DB derivations are the same identity.

# SHA-256 of the canonical baseline_unit_square under recipe v2 (the golden-vector
# corpus, scripts/data/dedup_golden_vectors_v2.json).
_BASELINE_UNIT_SQUARE_SHA256 = "169dc6c3af8c30d4a0b3436d1bbbde865626ff1d7e48195b14fcf563dc4663c1"


def test_geoid_from_geom_hash_is_version_8_and_rfc4122_variant():
    geoid = identifiers.geoid_from_geom_hash(bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256))
    assert geoid.version == 8
    assert geoid.variant == uuid.RFC_4122


def test_geoid_from_geom_hash_is_deterministic():
    digest = bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256)
    assert identifiers.geoid_from_geom_hash(digest) == identifiers.geoid_from_geom_hash(digest)


def test_geoid_from_geom_hash_known_answer():
    # Pins the exact byte layout (first 16 bytes of the SHA-256, version=8 + variant
    # stamped) so any change to the stamping surfaces as identity drift.
    geoid = identifiers.geoid_from_geom_hash(bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256))
    assert str(geoid) == "169dc6c3-af8c-80d4-a0b3-436d1bbbde86"


def test_geoid_from_geom_hash_distinct_digests_give_distinct_geoids():
    a = identifiers.geoid_from_geom_hash(bytes.fromhex("00" * 32))
    b = identifiers.geoid_from_geom_hash(bytes.fromhex("ff" * 32))
    assert a != b


def test_geoid_from_geom_hash_uses_only_first_16_bytes():
    # The geoid is a 128-bit truncation: bytes past the 16th never affect it.
    prefix = "11" * 16
    geoid_a = identifiers.geoid_from_geom_hash(bytes.fromhex(prefix + "00" * 16))
    geoid_b = identifiers.geoid_from_geom_hash(bytes.fromhex(prefix + "ff" * 16))
    assert geoid_a == geoid_b


def test_geoid_from_geom_hash_rejects_short_input():
    with pytest.raises(ValueError):
        identifiers.geoid_from_geom_hash(b"\x00" * 15)

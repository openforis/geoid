"""Unit tests for registry_service error-handling branches (no DB, no Docker).

Covers the two "should not happen" paths hardened in the review:
- a dedup loser whose incumbent collection never materialises → RegistryConsistencyError
- an unrecognised integrity constraint in bulk → an honest ``internal_error`` reject
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from geoid.config import Settings
from geoid.deps import Principal
from geoid.models import (
    PK_GEOID_REGISTRY,
    UQ_GEOID_REGISTRY_GEOM_HASH,
    UQ_PLACE_EXTERNAL_ID,
    Collection,
)
from geoid.repositories import place_repo
from geoid.repositories.place_repo import InsertResult
from geoid.schemas.place import PlaceCreate
from geoid.services import registry_service
from geoid.services.exceptions import (
    GeometryInvalidError,
    RegistryConsistencyError,
)

pytestmark = pytest.mark.unit

_SQUARE = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
    },
    "properties": {},
}


async def test_create_place_raises_registry_consistency_when_incumbent_missing(monkeypatch):
    # Force the drift: insert_place reports a dedup loser (created=False) whose
    # incumbent collection slug never resolved (None). create_place must raise the
    # structured RegistryConsistencyError carrying the geoid rather than return a
    # 201 whose geoid does not resolve.
    drift_geoid = uuid.uuid4()

    async def _drift(*args, **kwargs):
        return InsertResult(geoid=drift_geoid, created=False, collection_slug=None)

    monkeypatch.setattr(place_repo, "insert_place", _drift)

    feature = PlaceCreate.model_validate(_SQUARE)
    collection = Collection(id=uuid.uuid4(), catalog_id=uuid.uuid4(), slug="x", public_write=True)

    with pytest.raises(RegistryConsistencyError) as exc_info:
        await registry_service.create_place(
            None,  # session is unused once insert_place is faked
            settings=Settings(),
            principal=Principal.admin(),
            collection=collection,
            feature=feature,
        )
    assert exc_info.value.geoid == drift_geoid


# --- DBAPIError sqlstate discrimination (H2): only known ST_GeomFromGeoJSON ----
# parse states become the client-facing 422; anything else stays loud.


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("boom")
        self.sqlstate = sqlstate


class _FakeSession:
    """Just enough session for the error paths: rollback + SAVEPOINT context."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True

    def begin_nested(self):
        class _Nested:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Nested()


def _collection() -> Collection:
    return Collection(id=uuid.uuid4(), catalog_id=uuid.uuid4(), slug="x", public_write=True)


def _raise_dbapi(sqlstate: str):
    async def _raise(*args, **kwargs):
        raise DBAPIError("INSERT ...", {}, _FakeDriverError(sqlstate))

    return _raise


async def test_unknown_dbapi_sqlstate_reraises_instead_of_422(monkeypatch):
    # 42883 (UndefinedFunction — the June-15 schema-drift outage signature) must NOT
    # be masked as the client's "unparseable GeoJSON": re-raise for a loud 500.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("42883"))
    with pytest.raises(DBAPIError):
        await registry_service.create_place(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            feature=PlaceCreate.model_validate(_SQUARE),
        )


async def test_geojson_parse_sqlstate_maps_to_geometry_invalid(monkeypatch):
    # XX000 (PostGIS lwgeom parse error) IS the malformed-GeoJSON class -> 422.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("XX000"))
    session = _FakeSession()
    with pytest.raises(GeometryInvalidError):
        await registry_service.create_place(
            session,
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            feature=PlaceCreate.model_validate(_SQUARE),
        )
    assert session.rolled_back is True


async def test_bulk_unknown_dbapi_sqlstate_aborts_the_batch(monkeypatch):
    # In bulk, a programming/schema failure must abort the whole batch loudly — a
    # mislabelled per-feature invalid_geometry would hide a server-side outage.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("42883"))
    with pytest.raises(DBAPIError):
        await registry_service.create_places_bulk(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            features=[_SQUARE],
        )


async def test_bulk_geojson_parse_sqlstate_rejects_the_row(monkeypatch):
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("22023"))
    report = await registry_service.create_places_bulk(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.admin(),
        collection=_collection(),
        features=[_SQUARE],
    )
    assert report.summary.rejected == 1
    assert report.rejected[0].reason == "invalid_geometry"


# --- registry unique-index race: one retry converges on the idempotent result ----
# The arbiter CTE names only the geom_hash UNIQUE; an identical-geometry loser can
# trip the registry PK instead. The 23505 fires only after the winner commits, so
# a single retry must recover the incumbent geoid.


def _unique_violation(constraint: str) -> IntegrityError:
    # Mirrors the asyncpg shape pg_fields() reads: sqlstate on exc.orig,
    # constraint_name on exc.orig.__cause__.
    cause = _FakeDriverError("23505")
    cause.constraint_name = constraint
    orig = Exception("duplicate key value violates unique constraint")
    orig.__cause__ = cause
    return IntegrityError("INSERT ...", {}, orig)


def _raise_once_then_loser(constraint: str, incumbent: InsertResult):
    calls = {"n": 0}

    async def _insert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _unique_violation(constraint)
        return incumbent

    return _insert, calls


def _incumbent_loser() -> InsertResult:
    return InsertResult(geoid=uuid.uuid4(), created=False, collection_slug="public")


@pytest.mark.parametrize("constraint", [PK_GEOID_REGISTRY, UQ_GEOID_REGISTRY_GEOM_HASH])
async def test_create_place_retries_once_after_losing_registry_race(monkeypatch, constraint):
    incumbent = _incumbent_loser()
    insert, calls = _raise_once_then_loser(constraint, incumbent)
    monkeypatch.setattr(place_repo, "insert_place", insert)
    session = _FakeSession()

    result = await registry_service.create_place(
        session,
        settings=Settings(),
        principal=Principal.admin(),
        collection=_collection(),
        feature=PlaceCreate.model_validate(_SQUARE),
    )

    assert calls["n"] == 2
    assert session.rolled_back is True
    assert result.geoid == str(incumbent.geoid)


async def test_create_place_does_not_retry_external_id_conflict(monkeypatch):
    insert, calls = _raise_once_then_loser(UQ_PLACE_EXTERNAL_ID, _incumbent_loser())
    monkeypatch.setattr(place_repo, "insert_place", insert)

    with pytest.raises(IntegrityError):
        await registry_service.create_place(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            feature=PlaceCreate.model_validate(_SQUARE),
        )
    assert calls["n"] == 1


@pytest.mark.parametrize("constraint", [PK_GEOID_REGISTRY, UQ_GEOID_REGISTRY_GEOM_HASH])
async def test_bulk_retries_registry_race_and_accepts_the_incumbent(monkeypatch, constraint):
    incumbent = _incumbent_loser()
    insert, calls = _raise_once_then_loser(constraint, incumbent)
    monkeypatch.setattr(place_repo, "insert_place", insert)

    report = await registry_service.create_places_bulk(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.admin(),
        collection=_collection(),
        features=[_SQUARE],
    )

    assert calls["n"] == 2
    assert report.summary.accepted == 1
    assert report.summary.rejected == 0
    assert report.accepted[0].geoid == str(incumbent.geoid)


async def test_bulk_second_race_loss_falls_back_to_geoid_conflict_reject(monkeypatch):
    # Retry is bounded: a second loss classifies via the existing backstop instead
    # of looping.
    calls = {"n": 0}

    async def _always_race(*args, **kwargs):
        calls["n"] += 1
        raise _unique_violation(PK_GEOID_REGISTRY)

    monkeypatch.setattr(place_repo, "insert_place", _always_race)

    report = await registry_service.create_places_bulk(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.admin(),
        collection=_collection(),
        features=[_SQUARE],
    )

    assert calls["n"] == 2
    assert report.rejected[0].reason == "geoid_conflict"


# --- idempotent mint: a dedup loser answers exactly like a winner ----------------
# The client rule: a repeat upload returns the already-minted geoid with no signal
# that it was a duplicate. The incumbent's collection never reaches the response.


async def test_dedup_loser_returns_the_incumbent_geoid_like_a_first_mint(monkeypatch):
    incumbent = InsertResult(geoid=uuid.uuid4(), created=False, collection_slug="somewhere-private")

    async def _loser(*args, **kwargs):
        return incumbent

    monkeypatch.setattr(place_repo, "insert_place", _loser)

    result = await registry_service.create_place(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.anonymous(),
        collection=_collection(),
        feature=PlaceCreate.model_validate(_SQUARE),
    )

    assert result.geoid == str(incumbent.geoid)
    assert result.uri.endswith(str(incumbent.geoid))
    # The incumbent's collection is not in the body — that is what dissolves the
    # probe oracle: a repeat POST is byte-shape-identical to a first mint.
    assert "collection" not in result.model_dump()


async def test_bulk_dedup_loser_is_accepted_not_rejected(monkeypatch):
    incumbent = InsertResult(geoid=uuid.uuid4(), created=False, collection_slug="somewhere-private")

    async def _loser(*args, **kwargs):
        return incumbent

    monkeypatch.setattr(place_repo, "insert_place", _loser)

    report = await registry_service.create_places_bulk(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.anonymous(),
        collection=_collection(),
        features=[_SQUARE, _SQUARE],
    )

    assert report.summary.accepted == 2
    assert report.summary.rejected == 0
    assert [row.geoid for row in report.accepted] == [str(incumbent.geoid)] * 2


async def test_bulk_dedup_loser_without_incumbent_aborts_the_batch(monkeypatch):
    drift_geoid = uuid.uuid4()

    async def _drift(*args, **kwargs):
        return InsertResult(geoid=drift_geoid, created=False, collection_slug=None)

    monkeypatch.setattr(place_repo, "insert_place", _drift)

    with pytest.raises(RegistryConsistencyError):
        await registry_service.create_places_bulk(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            features=[_SQUARE],
        )


async def test_classify_integrity_unknown_constraint_is_internal_error(monkeypatch):
    # An unrecognised constraint must surface honestly as internal_error (and be
    # logged), NOT be mislabelled invalid_geometry. The fallthrough never touches the
    # session, so None is safe here.
    monkeypatch.setattr(
        registry_service, "pg_fields", lambda exc: ("some_unexpected_constraint", "99999")
    )
    exc = IntegrityError("INSERT ...", {}, Exception("boom"))

    rejected = await registry_service._classify_integrity(
        None, index=3, external_id="ext-9", geojson="{}", exc=exc
    )

    assert rejected.reason == "internal_error"
    assert rejected.index == 3
    assert rejected.external_id == "ext-9"
    assert "some_unexpected_constraint" in (rejected.detail or "")

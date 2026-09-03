"""Identity-lattice degeneracy (recipe v2, ADR-007) through the live write path.

Two-layer enforcement, both pinned here:

1. **Schema pre-check** (``PlaceCreate``): the normal path — a geometry that
   degenerates on the 1e-7 lattice answers a clean 422 on the single route and a
   ``schema_invalid`` reject on bulk, before any DB round-trip.
2. **DB backstop** (SQLSTATE ``GD001`` from ``geoid_canon_ring_v2``): writes that
   bypass the schema (raw repo calls) still cannot store a degenerate geometry —
   and this empirically verifies asyncpg surfaces the custom SQLSTATE through the
   arbiter INSERT, the one driver behavior the design leaned on.
"""

from __future__ import annotations

import json

import pytest
from geojson_pydantic.geometries import Polygon
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from geoid.config import get_settings
from geoid.deps import Principal
from geoid.models import Collection
from geoid.repositories import place_repo
from geoid.repositories._pg_errors import SQLSTATE_DEGENERATE_GEOMETRY, sqlstate_of
from geoid.schemas.place import PlaceCreate
from geoid.services import registry_service
from geoid.services.exceptions import GeometryInvalidError

pytestmark = pytest.mark.integration

# A valid polygon whose ring collapses to 2 distinct lattice vertices at 1e-7.
_SLIVER_COORDS = [[[0, 0], [1, 0], [1, 1e-8], [0, 1e-8], [0, 0]]]
_GOOD_COORDS = [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]]


def _feature(coords) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {},
    }


def _constructed_sliver() -> PlaceCreate:
    """A degenerate feature built WITHOUT validation (model_construct), so it
    reaches the DB and exercises the GD001 backstop the schema normally hides."""
    geometry = Polygon.model_construct(type="Polygon", coordinates=_SLIVER_COORDS)
    return PlaceCreate.model_construct(type="Feature", geometry=geometry, properties={}, id=None)


async def _public_collection(session) -> Collection:
    return (
        await session.execute(select(Collection).where(Collection.slug == "public"))
    ).scalar_one()


async def test_single_post_of_degenerate_geometry_answers_422(client):
    resp = await client.post("/collections/public/items", json=_feature(_SLIVER_COORDS))
    assert resp.status_code == 422
    assert "identity precision" in resp.text


async def test_bulk_degenerate_feature_is_one_schema_invalid_reject(client):
    body = {
        "type": "FeatureCollection",
        "features": [_feature(_GOOD_COORDS), _feature(_SLIVER_COORDS)],
    }
    resp = await client.post("/collections/public/items/bulk", json=body)
    assert resp.status_code == 200
    report = resp.json()
    assert report["summary"] == {"received": 2, "accepted": 1, "rejected": 1}
    assert report["accepted"][0]["index"] == 0
    (rejected,) = report["rejected"]
    assert rejected["index"] == 1
    assert rejected["reason"] == "schema_invalid"
    assert "identity precision" in rejected["detail"]


async def test_repo_insert_of_degenerate_geometry_raises_gd001(session):
    # The driver-level pin: the arbiter CTE's hash call raises the custom
    # SQLSTATE and asyncpg surfaces it on the wrapped error.
    collection = await _public_collection(session)
    geojson = json.dumps({"type": "Polygon", "coordinates": _SLIVER_COORDS})
    with pytest.raises(DBAPIError) as exc_info:
        await place_repo.insert_place(
            session,
            collection_id=collection.id,
            geojson=geojson,
            external_id=None,
            provenance={},
            originating_instance="test-instance",
        )
    assert sqlstate_of(exc_info.value) == SQLSTATE_DEGENERATE_GEOMETRY
    await session.rollback()


async def test_service_backstop_maps_gd001_to_the_422_error(session):
    # Bypass the schema (model_construct) so the DB backstop must answer: the
    # service's DBAPIError branch maps GD001 -> GeometryInvalidError (422).
    collection = await _public_collection(session)
    with pytest.raises(GeometryInvalidError, match="identity precision"):
        await registry_service.create_place(
            session,
            settings=get_settings(),
            principal=Principal.anonymous(),
            collection=collection,
            feature=_constructed_sliver(),
        )


async def test_bulk_service_backstop_rejects_the_row_not_the_batch(session):
    # The bulk twin: the SAVEPOINT rolls back just the degenerate row and the
    # GD001 branch classifies it invalid_geometry — the batch survives.
    collection = await _public_collection(session)
    outcome = await registry_service._mint_one(
        session,
        settings=get_settings(),
        principal=Principal.anonymous(),
        collection=collection,
        index=0,
        feature=_constructed_sliver(),
    )
    assert outcome.reason == "invalid_geometry"
    assert "identity precision" in outcome.detail

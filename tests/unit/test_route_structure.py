"""Structural guards over the app's route table.

M11: the root ``/{geoid}`` resolver is a single-segment catch-all whose safety
depends on registration ORDER (literal routes must come first). That was
convention held by a comment; these tests make it structural.

M12: ``require_principal`` deliberately resolves to an anonymous principal, so a
future mutating route that forgets its authorization check would be silently
anonymous-writable. The inventory test forces every new mutating route through a
review: it fails until the route is added here (with its authz mechanism named).
"""

from __future__ import annotations

import json

import pytest
from fastapi.routing import APIRoute

from geoid.main import create_app
from geoid.schemas.collection import CollectionCreate
from geoid.schemas.place import BulkFeatureCollection, PlaceCreate

pytestmark = pytest.mark.unit


def _paths(app) -> list[str]:
    return [route.path for route in app.routes]


def test_no_literal_single_segment_route_registers_after_the_geoid_catchall():
    paths = _paths(create_app())
    catchall = paths.index("/{geoid}")
    offenders = [
        path
        for index, path in enumerate(paths)
        if index > catchall and "{" not in path and path.count("/") == 1
    ]
    assert offenders == [], (
        f"literal single-segment routes registered AFTER the /{{geoid}} catch-all "
        f"(they would be shadowed): {offenders}"
    )


def test_all_known_literal_single_segment_routes_precede_the_catchall():
    paths = _paths(create_app())
    catchall = paths.index("/{geoid}")
    before = set(paths[:catchall])
    assert {
        "/",
        "/conformance",
        "/collections",
        "/health",
        "/items",
        "/docs",
        "/redoc",
        "/openapi.json",
    } <= before


def test_the_public_schema_is_exactly_the_three_client_facing_operations():
    # The executable spec for the narrowed public surface (2026-07-25 client
    # ruling): mint, bulk mint, resolve. Everything else stays live but hidden.
    assert set(create_app().openapi()["paths"]) == {"/items", "/items/bulk", "/{geoid}"}


def test_the_three_public_operations_declare_no_security_requirement():
    schema = create_app().openapi()
    operations = (
        schema["paths"]["/items"]["post"],
        schema["paths"]["/items/bulk"]["post"],
        schema["paths"]["/{geoid}"]["get"],
    )
    assert all("security" not in operation for operation in operations)


@pytest.mark.parametrize("suffix", ["", "/bulk"])
def test_public_and_scoped_writes_are_the_same_endpoint_object(suffix):
    # The mechanism itself, not just its observable behaviour: ONE handler included
    # twice. Reintroducing a wrapper handler that merely calls the other would keep
    # every behavioural test green while letting the two HTTP contracts drift.
    routes = {route.path: route for route in create_app().routes if isinstance(route, APIRoute)}
    public = routes[f"/items{suffix}"]
    scoped = routes[f"/collections/{{collection_id}}/items{suffix}"]

    assert public.endpoint is scoped.endpoint
    assert public.response_model is scoped.response_model
    assert public.status_code == scoped.status_code


def test_public_read_is_documented_as_an_inert_compatibility_field():
    # Collection routes are intentionally hidden, so CollectionCreate is not
    # necessarily reachable from application OpenAPI. Verify its own public schema.
    field = CollectionCreate.model_json_schema()["properties"]["public_read"]
    assert field["default"] is True
    assert "Inert compatibility field" in field["description"]
    assert "authorizes no reads or writes" in field["description"]


def test_registry_post_body_schemas_carry_executable_examples():
    # The body examples must stay EXECUTABLE (first click mints) and properties-free
    # (an omitted properties member is the point of the leniency). Validating them
    # through the models themselves pins executability without duplicating literals;
    # distinct geometries pin that single-then-bulk first clicks mint distinct geoids.
    schemas = create_app().openapi()["components"]["schemas"]

    place = schemas["PlaceCreate"]
    assert place["required"] == ["type", "geometry"]
    assert "properties" not in place["example"]
    PlaceCreate.model_validate(place["example"])

    bulk = schemas["BulkFeatureCollection"]
    BulkFeatureCollection.model_validate(bulk["example"])
    for feature in bulk["example"]["features"]:
        assert "properties" not in feature
        PlaceCreate.model_validate(feature)

    geometries = [place["example"]["geometry"]] + [
        f["geometry"] for f in bulk["example"]["features"]
    ]
    assert len({json.dumps(g, sort_keys=True) for g in geometries}) == len(geometries)


@pytest.mark.parametrize("path", ["/items", "/items/bulk"])
def test_registry_post_body_has_no_examples_dropdown(path):
    # Mirror of the path-param pin: the body must seed from `schema.example` only —
    # an `examples` map would render as a Swagger dropdown.
    content = create_app().openapi()["paths"][path]["post"]["requestBody"]["content"]
    assert "examples" not in content["application/json"]


def test_every_mutating_route_is_in_the_authorized_inventory():
    app = create_app()
    mutating = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    # Every entry's authorization mechanism, verified at review time:
    #   items / items/bulk  -> registry_service._authorize_write (service layer)
    #   /items[/bulk]       -> the same handler objects, included twice
    #   grants routes       -> api.grants._require_manageable (anon→401, owner|sysadmin)
    #   /manage/*           -> require_admin router dependency
    # Adding a mutating route? Wire its authz, then extend this set.
    assert mutating == {
        ("POST", "/collections/{collection_id}/items"),
        ("POST", "/collections/{collection_id}/items/bulk"),
        ("POST", "/items"),
        ("POST", "/items/bulk"),
        ("POST", "/collections/{collection_id}/grants"),
        ("DELETE", "/collections/{collection_id}/grants/{email}"),
        ("POST", "/manage/collections"),
    }

"""Write / registry router — the product surface.

POST a polygon → ``{geoid, uri}``; resolve durably by geoid; resolve by
``(external_id, collection)``. Anonymous POSTs are allowed into ``public_write``
collections via the shared registry service (no special code path).

Three operations are public (visible in ``/docs``): ``POST /items``,
``POST /items/bulk`` and the resolver ``GET /{geoid}``. They are deliberately
authentication-invariant: Authorization is ignored, public mints record no caller
identity, and the resolver always returns the anonymous representation. The
collection-scoped originals stay live and are simply hidden from the schema —
hiding is cosmetic, never an authorization control.

Each write operation is ONE handler served at TWO paths: ``_writes`` is included
into ``router`` twice (bare for the public paths, prefixed for the collection-scoped
ones), so both URL shapes share a single response model, status code, dependency
graph and validation contract — they cannot drift apart.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.api.format_param import output_format
from geoid.api.responses import GeoJSONResponse, WKTResponse, feature_response, resolver_cache
from geoid.config import Settings, get_settings
from geoid.db import get_session
from geoid.deps import (
    Principal,
    bearer_scheme,
    get_jwks_client,
    principal_from_credentials,
    require_principal,
)
from geoid.domain.geometry_format import GeometryFormat
from geoid.repositories import collection_repo, place_repo
from geoid.schemas.ogc import FeatureModel
from geoid.schemas.place import (
    BulkFeatureCollection,
    BulkReport,
    MintResponse,
    PlaceCreate,
    PlaceRecord,
)
from geoid.services import authz_service, ogc_service, registry_service
from geoid.services.exceptions import (
    BulkLimitExceededError,
    CollectionNotFoundError,
    PlaceNotFoundError,
    PublicExternalIdLookupError,
)

router = APIRouter(tags=["registry"])


def _created_by(row: dict) -> str | None:
    """The mint-time creator ``sub`` recorded in provenance (None for anonymous mints)."""
    return (row.get("provenance") or {}).get("created_by")


# --- the write operations: one handler each, two paths -----------------------

_writes = APIRouter()


async def _target_collection(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """The collection a write targets: the path's when scoped, else the public one.

    One handler serves both URL shapes, so the scoped path parameter is read off the
    request instead of the signature — declaring it would make ``collection_id`` a
    required *query* parameter on the public ``/items`` paths.
    """
    return request.path_params.get("collection_id", settings.public_collection)


_CollectionTarget = Annotated[str, Depends(_target_collection)]


async def _target_principal(
    request: Request,
    collection_id: _CollectionTarget,
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Anonymous for the public collection; authenticated for managed ones.

    Credential extraction is conditional rather than a nested dependency because
    FastAPI resolves every declared dependency before entering this function. If
    ``bearer_scheme`` were declared with ``Depends``, a malformed credential could
    reject a public request before this public-collection branch could ignore it.
    """
    if collection_id == settings.public_collection:
        return Principal.anonymous()
    credentials = await bearer_scheme(request)
    # This is intentionally lazy: a public request must not touch authentication
    # infrastructure at all. Honor FastAPI's documented dependency-overrides map
    # when resolving the provider manually for the conditional managed branch.
    jwks_provider = request.app.dependency_overrides.get(get_jwks_client, get_jwks_client)
    jwks_client = jwks_provider()
    return await principal_from_credentials(credentials, settings, jwks_client)


_TargetPrincipal = Annotated[Principal, Depends(_target_principal)]


@_writes.post(
    "/items",
    response_model=MintResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a geoid for a geometry",
    description=(
        "Submit one GeoJSON Feature and get back its geoid — a permanent, globally "
        "unique identifier derived from the geometry itself. Submitting the same "
        "geometry again always returns the same geoid."
    ),
    responses={
        status.HTTP_201_CREATED: {
            "headers": {
                "Location": {
                    "description": (
                        "Durable resolver URI of the minted geoid (OGC API - "
                        "Features Part 4, Requirement 6: a 201 carries a Location header)."
                    ),
                    "schema": {"type": "string", "format": "uri"},
                }
            }
        },
    },
)
async def create_item(
    collection_id: _CollectionTarget,
    feature: PlaceCreate,
    response: Response,
    principal: _TargetPrincipal,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MintResponse:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    result = await registry_service.create_place(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        feature=feature,
    )
    response.headers["Location"] = result.uri
    return result


@_writes.post(
    "/items/bulk",
    response_model=BulkReport,
    status_code=status.HTTP_200_OK,
    summary="Mint geoids for many geometries at once",
    description=(
        "Submit a GeoJSON FeatureCollection and get one geoid per feature, in the "
        "same request. The response reports every feature: those that got a geoid "
        "and those that could not be processed, with the reason. Features are "
        "durable only once the report is received — a timeout or disconnect before "
        "then keeps nothing; simply submit again."
    ),
)
async def create_items_bulk(
    collection_id: _CollectionTarget,
    body: BulkFeatureCollection,
    principal: _TargetPrincipal,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BulkReport:
    """Bulk-mint geoids from a GeoJSON FeatureCollection, in-request.

    Partial success at the *report* level, atomic at the *transaction* level: the
    whole batch runs in ONE database transaction, so a request timeout or client
    disconnect before the response discards ALL rows — including the ones the report
    would have listed as accepted. Rows are durable only once the 200 report is
    received. There is no resumability; re-submit the batch (already-minted features
    come back accepted with their existing geoid).
    """
    # A write bound MUST error, never truncate: too many features rejects the
    # whole request (413) before any insert. Auth (anon → public_write) is checked
    # once up front in the service, since it depends on principal + collection only.
    if len(body.features) > settings.bulk_max_features:
        raise BulkLimitExceededError(len(body.features), settings.bulk_max_features)
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    return await registry_service.create_places_bulk(
        session,
        settings=settings,
        principal=principal,
        collection=collection,
        features=body.features,
    )


@router.get(
    "/me/geoids",
    response_model=list[PlaceRecord],
    summary="List the geoids the authenticated caller has minted (newest first)",
    include_in_schema=False,
    description=(
        "Keyed on the caller's stable subject (Keycloak `sub`) recorded at mint "
        "time. Anonymous callers have no identity to list — 401. Geometry-free "
        "records; resolve a geoid for the feature itself."
    ),
)
async def list_my_geoids(
    principal: Principal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[PlaceRecord]:
    if principal.is_anonymous:
        # 401 (not 403): same anonymous split as grants._require_manageable.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    rows = await place_repo.list_by_creator(session, principal.subject, limit=limit, offset=offset)
    return [PlaceRecord.from_row(row, base_url=settings.base_url_clean) for row in rows]


# The public paths come from the bare include, the collection-scoped ones from the
# prefixed include — same handlers, so the two shapes cannot drift. Both run BEFORE
# the /{geoid} catch-all below, so the literal single segment "items" is matched as
# a route, not parsed as a geoid.
# The scoped include is hidden from the schema; were it ever unhidden, its
# {collection_id} would go undocumented — it is deliberately absent from the
# handler signatures (see _target_collection).
router.include_router(_writes)
router.include_router(_writes, prefix="/collections/{collection_id}", include_in_schema=False)


@router.get(
    "/{geoid}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Durable geoid resolver",
    responses={200: {"content": {"text/plain": {}}}},
)
async def resolve_geoid(
    geoid: uuid.UUID,
    response: Response,
    fmt: GeometryFormat = Depends(output_format),
    if_none_match: str | None = Header(default=None, include_in_schema=False),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse | Response:
    # This public operation is authentication-invariant: Authorization is not a
    # dependency and therefore cannot change the body, cache policy, or error
    # contract. Every caller receives the anonymous geometry + {geoid, uri} view.
    row = await place_repo.get_by_geoid(session, geoid)
    if row is None:
        raise PlaceNotFoundError(str(geoid))
    cache = resolver_cache(
        settings=settings,
        principal=Principal.anonymous(),
        geoid=geoid,
        fmt=fmt,
        if_none_match=if_none_match,
        vary_authorization=False,
    )
    if isinstance(cache, Response):
        return cache
    feature = ogc_service.build_feature(settings, row, full=False)
    # BOTH header merges are required: FastAPI copies the injected response's
    # headers only on the model-return (GeoJSON) path, never onto the returned
    # WKTResponse — that one gets them via feature_response(headers=...).
    response.headers.update(cache)
    return feature_response(feature, fmt, headers=cache)


# --- internal: live, hidden from /docs ---------------------------------------


@router.get(
    "/collections/{collection_id}/external/{external_id}",
    response_model=FeatureModel,
    response_class=GeoJSONResponse,
    summary="Resolve a place by (external_id, collection) — answers like the geoid resolver",
    include_in_schema=False,
    description=(
        "Behaves exactly like `GET /{geoid}`: an existing (collection, "
        "external_id) answers 200 to every caller — the full feature for "
        "sysadmin / the creator / grant holders, the geometry-only masked body "
        "for everyone else. 404 only for an unknown collection or external_id. "
        "Not available in the reserved public collection (its external_id "
        "values are stored but neither unique nor resolvable) — answers 400 "
        "with an explicit message there."
    ),
    responses={200: {"content": {"text/plain": {}}}},
)
async def resolve_by_external_id(
    collection_id: str,
    external_id: str,
    response: Response,
    fmt: GeometryFormat = Depends(output_format),
    if_none_match: str | None = Header(default=None, include_in_schema=False),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeatureModel | WKTResponse | Response:
    collection = await collection_repo.get_by_slug(session, collection_id)
    if collection is None:
        raise CollectionNotFoundError(collection_id)
    # The reserved public collection stores external_id without uniqueness
    # (migration 0012 excludes it from the unique index), so a lookup there
    # could match many rows — an explicit 400 (user ruling 2026-07-16), never
    # a masking 404: this API reserves 404 for a genuinely unknown id.
    if collection.slug == settings.public_collection:
        raise PublicExternalIdLookupError(collection.slug)
    row = await place_repo.get_by_external_id(session, collection.id, external_id)
    if row is None:
        raise PlaceNotFoundError(f"{collection_id}/{external_id}")
    cache = resolver_cache(
        settings=settings,
        principal=principal,
        geoid=row["geoid"],
        fmt=fmt,
        if_none_match=if_none_match,
    )
    if isinstance(cache, Response):
        return cache
    # Existence is never masked (client ruling 2026-07-09 round 2 — consistent
    # with the geoid resolver); only the BODY is caller-aware, on the same
    # query-avoiding order as resolve_geoid.
    created_by = _created_by(row)
    full = authz_service.can_see_metadata(principal, created_by, None)
    if not full and not principal.is_anonymous:
        grant = await authz_service.load_caller_grant(
            session, principal, collection.id, collection.slug
        )
        full = authz_service.can_see_metadata(principal, created_by, grant)
    feature = ogc_service.build_feature(settings, row, full=full)
    # Same dual merge as resolve_geoid (injected-response headers reach only the
    # GeoJSON model path; the WKT Response takes them via feature_response).
    response.headers.update(cache)
    return feature_response(feature, fmt, headers=cache)

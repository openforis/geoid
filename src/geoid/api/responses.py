"""Custom responses.

OGC API Features serves GeoJSON features with the ``application/geo+json`` media
type (not ``application/json``); some clients content-negotiate on it. We subclass
``JSONResponse`` only to set that media type — FastAPI already serializes the
``response_model`` efficiently, so no faster JSON library is needed here.

Also the single home of the flag-gated resolver HTTP-cache trial
(:func:`resolver_cache`): the geoid and external-id resolvers delegate here so
the header semantics, ETag shape, and RFC 9110 If-None-Match matching cannot
drift between routes even though only the latter is caller-aware.
"""

from __future__ import annotations

import hashlib
import uuid
from functools import lru_cache

from fastapi import Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from geoid import __version__
from geoid.config import Settings
from geoid.deps import Principal
from geoid.domain.geometry_format import GeometryFormat
from geoid.schemas.ogc import FeatureModel
from geoid.services import ogc_service


class GeoJSONResponse(JSONResponse):
    media_type = "application/geo+json"


class WKTResponse(PlainTextResponse):
    """Bare WKT geometry as ``text/plain`` (its own honest media type — WKT is not
    smuggled into a JSON envelope). Used for the single-feature resolver WKT path."""

    media_type = "text/plain"


def feature_response(
    feature: FeatureModel, fmt: GeometryFormat, headers: dict[str, str] | None = None
) -> FeatureModel | WKTResponse:
    """Render a single feature in the negotiated format (shared by all item routes).

    GeoJSON returns the model unchanged (FastAPI serialises it via the route's
    ``GeoJSONResponse``); WKT returns a bare ``text/plain`` body with the reciprocal
    GeoJSON alternate carried in the ``Link`` header. ``headers`` are merged onto
    the WKT response — required because FastAPI never copies the injected
    ``Response``'s headers onto a *returned* Response instance (only onto the
    model-return path), so the resolver cache headers must ride in here.
    """
    if fmt is GeometryFormat.WKT:
        return WKTResponse(
            ogc_service.feature_to_wkt(feature),
            headers={
                "Link": ogc_service.feature_geojson_alternate_header(feature),
                **(headers or {}),
            },
        )
    return feature


def resolver_cache(
    *,
    settings: Settings,
    principal: Principal,
    geoid: uuid.UUID,
    fmt: GeometryFormat,
    if_none_match: str | None,
    vary_authorization: bool = True,
) -> Response | dict[str, str]:
    """Cache directives for a public-resolver 200 (the flag-gated trial).

    Returns the headers to stamp on the 200: empty when
    ``resolver_cache_max_age`` is 0 (byte-identical behavior), ``private,
    no-store`` for authenticated callers (their ``If-None-Match`` is ignored —
    an authed body is caller-dependent, never revalidated), or the anonymous
    trio (strong per-representation ETag ``"{geoid}:{format}:{salt}"`` +
    ``Cache-Control: public`` + ``Vary``). ``vary_authorization=False`` is for an
    authentication-invariant resolver whose representation cannot vary by caller.
    A matching anonymous
    ``If-None-Match`` short-circuits to a ready-to-return empty 304 carrying
    those same headers.
    """
    max_age = settings.resolver_cache_max_age
    if max_age <= 0:
        return {}
    if not principal.is_anonymous:
        return {"Cache-Control": "private, no-store"}
    etag = f'"{geoid}:{fmt.value}:{_representation_salt(settings.base_url_clean)}"'
    headers = {
        "ETag": etag,
        "Cache-Control": f"public, max-age={max_age}",
        "Vary": "Accept, Authorization" if vary_authorization else "Accept",
    }
    if _if_none_match_hit(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return headers


@lru_cache
def _representation_salt(base_url_clean: str) -> str:
    """Rotates the strong ETag whenever the representation bytes can change
    for a reason other than (geoid, format): a code/masked-body-shape change
    (app version) or a base-URL change (the minted ``uri``/links). Without it a
    revalidating client's 304s would refresh freshness on a stale body forever
    — RFC 9110 requires a strong ETag to change with the representation."""
    return hashlib.sha256(f"{__version__}:{base_url_clean}".encode()).hexdigest()[:8]


def _if_none_match_hit(header_value: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: ``If-None-Match`` is a comma-separated list of
    entity-tags; a bare ``*`` matches any current representation; comparison is
    *weak* — a ``W/`` prefix on a candidate is ignored."""
    if not header_value:
        return False
    candidates = [candidate.strip() for candidate in header_value.split(",")]
    return any(candidate == "*" or candidate.removeprefix("W/") == etag for candidate in candidates)

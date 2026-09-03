"""The output-format query parameter, shared by every route that can emit WKT.

A FastAPI dependency so the alias, the precedence, the negotiation, and the 400
mapping live in exactly one place. Swagger advertises **``format``** (the friendly
name); the OGC ``f`` parameter is also accepted but hidden from the schema
(``include_in_schema=False``). ``format`` wins over ``f`` when both are given, and
either query param overrides the ``Accept`` header (``negotiate_format``). An
unknown value raises 400 — before any I/O.
"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request, status

from geoid.domain.geometry_format import GeometryFormat, negotiate_format

_FORMAT_DESC = "Output format: geojson (default) or wkt (vendor extension)"


def output_format(
    request: Request,
    fmt: str | None = Query(default=None, alias="format", description=_FORMAT_DESC),
    f: str | None = Query(default=None, include_in_schema=False),
) -> GeometryFormat:
    """Resolve the negotiated geometry format from ``?format=`` / ``?f=`` / ``Accept``."""
    try:
        return negotiate_format(fmt or f, request.headers.get("accept"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

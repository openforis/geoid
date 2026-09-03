"""Pydantic request/response schemas — the API's typed boundary.

Input geometry is validated by ``geojson-pydantic`` (RFC 7946 structure +
the supported-type discriminator: Point/MultiPoint/Polygon/MultiPolygon, lines and
GeometryCollection rejected) plus our own lon/lat bounds + non-empty checks;
``ST_IsValid`` is the final gate in the database (reject, don't repair).
"""

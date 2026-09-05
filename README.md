# GeoID

Anonymous, persistent identifiers for geospatial geometries.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python ≥ 3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](pyproject.toml)

A **geoid** is an immutable UUID derived from the geometry itself, so the same shape always yields
the same identifier on any deployment. GeoID is a Python service built with FastAPI, backed by
PostgreSQL with PostGIS, and distributed as a single Docker image.

Developed by FAO under the [Open Foris Initiative](https://openforis.org).

## Why GeoID

- **Deterministic** — the geoid is a content-addressed UUIDv8 (RFC 9562) computed from the
  canonical geometry, not a random or sequential id.
- **Idempotent** — re-submitting a geometry returns the same geoid with the same `201`; nothing is
  written twice.
- **Immutable** — records are insert-only. Corrections mint a new geoid; old ones resolve forever.
- **Anonymous** — minting and resolving need no account.
- **Standards-based** — GeoJSON (RFC 7946) in and out, WKT accepted, OGC API Features conventions.
- **Self-hostable** — environment-driven configuration; the only dependency is PostgreSQL + PostGIS.

## API

| Operation | Route | Returns |
|---|---|---|
| Mint | `POST /items` | `201` + `{geoid, uri, external_id}`, `Location` header |
| Bulk mint | `POST /items/bulk` | `200` + per-feature report |
| Resolve | `GET /{geoid}` | GeoJSON Feature, or WKT with `?format=wkt` |

Interactive docs live at `/docs` on a running instance. The examples below assume
`http://localhost:8000` (see [Quickstart](#quickstart)).

### Mint

```bash
curl -si http://localhost:8000/items -H 'Content-Type: application/json' -d '{
  "type": "Feature", "id": "my-plot-001",
  "geometry": {"type": "Polygon", "coordinates": [[
    [12.3456789, 45.6789012], [12.456789, 45.6789012], [12.456789, 45.7890123],
    [12.3456789, 45.7890123], [12.3456789, 45.6789012]]]}
}'
```

```
HTTP/1.1 201 Created
Location: http://localhost:8000/e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af

{"geoid":"e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af","uri":"http://localhost:8000/e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af","external_id":"my-plot-001"}
```

Run it again: same status, same body, no second row.

### Resolve

```bash
curl -s http://localhost:8000/e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af
```

```json
{"type": "Feature", "id": "e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af",
 "geometry": {"type": "Polygon", "coordinates": [[[12.3456789, 45.6789012], "…"]]},
 "properties": {"geoid": "e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af", "uri": "http://localhost:8000/e4b1a65e-a0cf-8738-a6c2-3c3b4250b0af"},
 "links": [{"rel": "self", "type": "application/geo+json", "href": "…"}, {"rel": "alternate", "type": "text/plain", "href": "…?f=wkt"}]}
```

Append `?format=wkt` to get the geometry as bare `text/plain` WKT.

### Bulk mint

Submit a GeoJSON FeatureCollection. Features are processed in order within the request, each one
minted or matched to its existing geoid exactly as a single mint would be. The response is always
`200` with a per-feature report: one bad feature never sinks the batch (partial success), and
identical geometries within a batch all receive the same geoid. The FAO-hosted instance accepts
up to 4,000 features per request; a self-hosted instance defaults to 1,000
(`GEOID_BULK_MAX_FEATURES`). Larger requests are refused with `413`, never truncated. Processing
time grows with batch size, so keep the client timeout generous for large batches. An
asynchronous, storage-based ingestion path for very large datasets (upload a file, then poll a
job) is planned but not available yet.

```bash
curl -s http://localhost:8000/items/bulk -H 'Content-Type: application/json' -d '{
  "type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [12.4, 45.7]}},
    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
  ]
}'
```

```json
{
  "summary": {"received": 2, "accepted": 1, "rejected": 1},
  "accepted": [{"index": 0, "geoid": "…", "uri": "…", "external_id": null}],
  "rejected": [{"index": 1, "reason": "schema_invalid", "detail": "…", "external_id": null}]
}
```

### What is accepted

- Geometry types `Point`, `MultiPoint`, `Polygon`, `MultiPolygon`; lines and collections are rejected.
- WGS 84 longitude/latitude, 2D only. Any Z ordinate is rejected.
- `geometry` as a GeoJSON object or as a WKT string.
- The Feature `id`, if present, is stored as `external_id` and echoed back.
- `properties` are accepted but never stored.
- Invalid or degenerate geometry answers `422`. Nothing is auto-repaired.

## How identity works

The geoid is a pure function of the geometry. Each coordinate is snapped to a 1e-7° lattice
(about 1 cm) as an integer; rings are normalised (orientation, start vertex, part order);
the result is serialised and hashed with SHA-256; the first 128 bits are stamped as a UUIDv8.

- **Exact match, not proximity.** Two shapes get one geoid only if they land on the same lattice
  cells. Nearby shapes are different places.
- **Engine-independent.** The canonical form uses integer arithmetic only, so it is identical on
  every PostGIS build.
- **Implemented in SQL, mirrored in Python.** The database computes the geoid; the pure-Python
  reference in `src/geoid/domain/geometry_identity.py` is pinned to it by golden vectors in the test suite.
- **Pinned example.** The unit square `(0 0, 1 0, 1 1, 0 1)` always mints
  `169dc6c3-af8c-80d4-a0b3-436d1bbbde86`.

Design records: [ADR-004](docs/adr/ADR-004-deterministic-geoid.md),
[ADR-007](docs/adr/ADR-007-identity-recipe-v2.md), [ADR-009](docs/adr/ADR-009-idempotent-mint-and-narrowed-public-surface.md).

## Quickstart

Prerequisites: Docker, and [uv](https://docs.astral.sh/uv/) for the development loop.

**Whole stack** (PostGIS, migrations, then the API on port 8000):

```bash
docker compose up --build
```

**Development loop** (database in Docker, API on the host with autoreload):

```bash
docker compose up -d db
uv sync
cp .env.example .env                 # GEOID_DATABASE_URL, GEOID_BASE_URL
uv run geoid migrate
uv run uvicorn geoid.main:app --reload
```

Open `http://localhost:8000/docs`. If host port 5432 is taken, use
`GEOID_DB_PORT=5433 docker compose up -d db`.

**Sample data:** `uv run python scripts/seed_samples.py` mints the plots in `samples/`, then shows
an idempotent repeat, a rejected self-intersecting polygon, and a resolve. See
[`samples/README.md`](samples/README.md).

## Configuration

All settings are environment variables prefixed `GEOID_`; [`.env.example`](.env.example) documents
every one. The ones that matter for a deployment:

| Variable | Purpose |
|---|---|
| `GEOID_DATABASE_URL` | asyncpg SQLAlchemy URL of the PostGIS database |
| `GEOID_BASE_URL` | Public origin used in minted URIs and links |
| `GEOID_ROOT_PATH` | Sub-path the API is mounted under behind a proxy (e.g. `/geoid`) |
| `GEOID_ENVIRONMENT` | `development` (default), `review` or `production` |
| `GEOID_PUBLIC_COLLECTION` | Slug of the collection that receives public mints |
| `GEOID_INSTANCE_ID` | This instance's id, recorded in provenance |
| `GEOID_BULK_MAX_FEATURES` | Cap per bulk request (default 1000); over it → `413` |
| `GEOID_OIDC_ISSUER`, `GEOID_OIDC_JWKS_URL` | OIDC provider; required outside `development` |

## Deployment

One image, two entrypoints: `geoid migrate` (apply schema migrations) and `geoid web` (serve).
Run migrate to completion before starting web. Behind a reverse proxy, set `GEOID_ROOT_PATH` to
the mounted prefix and have the proxy strip it before forwarding.

Sizing: bulk minting is synchronous, capped by `GEOID_BULK_MAX_FEATURES` (default 1,000). Raise it
together with the server request timeout and memory; the FAO-hosted instance runs 4,000 with a
600 s request timeout. Resolves are single indexed lookups.

## Project layout

```
src/geoid/{api,services,repositories,models,schemas}   routers → logic → SQL → ORM; DTOs
src/geoid/domain/     pure logic: identity recipe, identifiers, geometry codecs
migrations/           Alembic: schema, triggers, identity SQL (the source of truth)
scripts/              seed, smoke test, golden-vector check
samples/              example GeoJSON
tests/                unit + integration (testcontainers PostGIS)
docs/adr/             architecture decision records
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache-2.0 — see [`LICENSE`](LICENSE).

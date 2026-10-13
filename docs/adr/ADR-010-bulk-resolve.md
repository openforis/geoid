# ADR-010 — Bulk resolve: `POST /bulk_resolve`, a partial FeatureCollection, and its own cap

- **Status:** Accepted — 2026-09-24; path renamed from `/resolve` to `/bulk_resolve`
  2026-10-13.
- **Extends:** [ADR-009](ADR-009-idempotent-mint-and-narrowed-public-surface.md) (the public
  surface gains a fourth operation; its authentication-invariance applies unchanged)
- **Implemented by:** `api.places.resolve_geoids`, `repositories.place_repo.get_by_geoids`,
  `schemas.place.ResolveRequest` / `ResolveResponse`, `Settings.bulk_resolve_max_geoids`
- **Not changed:** `GET /{geoid}`, the identity recipe, the schema. No migration.

## Context

The only public read was `GET /{geoid}`: one geoid per request. Clients holding thousands of
geoids — typically the output of a bulk mint — need them back in one request, the read-side
counterpart of `POST /items/bulk`.

## Decision

### 1. `POST /bulk_resolve` with a JSON body, not `GET` with a query string

A geoid is 36 characters, so a query string carrying many of them outgrows the URL length that
proxies and servers commonly accept at a few hundred ids — and fails there with an error the
service cannot shape. A bulk `GET` also gains little from HTTP caching: each client asks for a
different set, so the cache would rarely hit. Caching stays where it pays, on the single-id
`GET /{geoid}`.

The route is a literal single segment at the application root, next to the resolver it extends.
It is registered before the `/{geoid}` catch-all, like every other literal single-segment route.

### 2. The body

`{"geoids": [...]}` — an object, not a bare array, so fields can be added without breaking callers.
A malformed id or an empty list rejects the whole request (422): the ids are the caller's input,
unlike a bulk mint's features, where one bad feature is a data condition reported per row.

### 3. Always 200: a partial FeatureCollection

The response is a GeoJSON FeatureCollection. Each found geoid appears once, in first-request order,
with exactly the body `GET /{geoid}` returns — both are built by `ogc_service.build_feature(...,
full=False)`, so the two resolvers cannot drift. Unknown ids go into `not_found`, a foreign member
(RFC 7946 §6.1). Duplicates collapse to one entry. An unknown id is a data condition, not a request
error, so the status is 200 even when nothing resolves.

Like `GET /{geoid}`, the route declares no principal dependency: authentication cannot change its
status, body or side effects (ADR-009 §4). It sets no cache headers.

The lookup is one query, `WHERE p.id = ANY(:geoids)` on the primary key.

### 4. Its own cap, `GEOID_BULK_RESOLVE_MAX_GEOIDS`

Over the cap the whole request is rejected with 413 before any lookup, the same body the bulk-mint
413 carries. The cap is separate from `GEOID_BULK_MAX_FEATURES` because the two paths cost
different things: a bulk mint runs hashing, dedup and an insert per feature inside one transaction,
while a bulk resolve is one indexed read plus serialisation. Sharing one number would hold reads to
the write path's limit.

What bounds a read is the response size. The body is built and sent whole, and a platform or proxy
that limits non-streamed responses caps the product of geoid count and geometry size — a cap that
fits small polygons can overflow with detailed ones. The cap is therefore sized against the largest
geometries expected. Streaming the FeatureCollection is the upgrade path if a larger cap is needed.

## Consequences

- The public surface is four operations: mint, bulk mint, resolve, bulk resolve.
- A bulk resolve response carries no caching; repeated reads of the same set cost a query each.
- GeoJSON only. A multi-feature WKT representation has no natural form and is not offered.

"""Service-layer exceptions, mapped to HTTP in ``api/errors.py``."""

from __future__ import annotations

import uuid


class GeoidServiceError(Exception):
    """Base class for service errors."""


class CollectionNotFoundError(GeoidServiceError):
    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"collection not found: {slug!r}")


class GeometryInvalidError(GeoidServiceError):
    """Geometry failed RFC 7946 / ST_IsValid (we reject, never repair)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AnonymousWriteForbiddenError(GeoidServiceError):
    """Anonymous POST to a collection that is not ``public_write``."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"anonymous writes are not permitted to collection {slug!r}")


class WriteNotAuthorizedError(GeoidServiceError):
    """Authenticated caller without an editor/owner grant writing to a non-open
    collection (403). The anonymous case keeps its own
    :class:`AnonymousWriteForbiddenError` so the 401-vs-403/message split is preserved.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"write access to collection {slug!r} requires an editor or owner grant")


class CollectionForbiddenError(GeoidServiceError):
    """Authenticated caller lacks owner/sysadmin rights to manage a collection (403)."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"managing collection {slug!r} requires the owner or sysadmin role")


class OwnerGrantForbiddenError(GeoidServiceError):
    """Granting the ``owner`` role is sysadmin-only (403).

    A collection owner may staff editors and viewers but never mint peer owners;
    demoting or revoking an existing owner stays an owner-level operation.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(
            f"granting the owner role on collection {slug!r} requires the sysadmin role"
        )


class LastOwnerGuardError(GeoidServiceError):
    """Revoking/demoting the LAST owner grant of a collection is blocked (409).

    Applies to everyone, sysadmin included — a sysadmin wanting the owner gone
    grants another owner first. Keeps the invariant one rule with no bypass tier.
    """

    def __init__(self, slug: str, email: str) -> None:
        self.slug = slug
        self.email = email
        super().__init__(
            f"{email!r} is the last owner of collection {slug!r}; "
            "grant another owner before revoking or demoting this one"
        )


class GrantNotFoundError(GeoidServiceError):
    """DELETE of a grant that does not exist (404)."""

    def __init__(self, slug: str, email: str) -> None:
        self.slug = slug
        self.email = email
        super().__init__(f"no grant for {email!r} on collection {slug!r}")


class PlaceNotFoundError(GeoidServiceError):
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"place not found: {identifier}")


class PublicExternalIdLookupError(GeoidServiceError):
    """external_id lookup is unsupported in the reserved public collection (400).

    Public external_id values are stored but not unique (migration 0012), so a
    lookup could match many rows — an explicit 400, never a masking 404.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"external_id lookup is not available in the public collection {slug!r}")


class BulkLimitExceededError(GeoidServiceError):
    """A bulk POST FeatureCollection exceeds ``GEOID_BULK_MAX_FEATURES`` (413).

    A *write* bound must error, never silently truncate, so the whole request is
    rejected with the count and the configured cap.
    """

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(
            f"bulk ingest rejected: {count} features exceeds the limit of {limit} "
            "(GEOID_BULK_MAX_FEATURES)"
        )


class RegistryConsistencyError(GeoidServiceError):
    """A dedup loser whose incumbent collection never materialised (500).

    Should not happen: the geoid is derived in-statement so a loser always carries
    it, and ``_resolve_incumbent`` recovers the collection slug across the rare
    concurrent pre-commit window. A persistent miss means genuine registry/recipe
    drift — the guard against returning a 201 whose geoid does not resolve, so it
    is surfaced as a structured 500 (carrying the geoid) instead.
    """

    def __init__(self, geoid: uuid.UUID) -> None:
        self.geoid = geoid
        super().__init__(f"registry inconsistency: incumbent collection missing for geoid {geoid}")

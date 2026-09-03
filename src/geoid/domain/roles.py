"""Per-collection authorization roles (pure; no DB, no FastAPI).

Three ranked grants — ``viewer`` < ``editor`` < ``owner`` — held in the GeoID DB
(``collection_grant``), distinct from the global ``sysadmin`` tier (Keycloak's
``geoid.sysadmin`` role) which bypasses all per-collection checks.
``role_at_least`` is the single comparison the authz service uses.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """A per-collection grant role. ``StrEnum`` so the value compares/serialises as the
    plain ``'viewer'`` / ``'editor'`` / ``'owner'`` stored in the DB."""

    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


# Higher number = more privilege. owner ⊃ editor ⊃ viewer.
_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.EDITOR: 2, Role.OWNER: 3}


def role_at_least(have: Role | str | None, need: Role | str) -> bool:
    """True iff the ``have`` role ranks at or above ``need``.

    ``have`` is ``None`` for a caller with no grant (→ always False). Unknown role
    strings (should never reach here — the DB CHECK constrains the column) are
    treated as no grant rather than raising.
    """
    if have is None:
        return False
    try:
        return _RANK[Role(have)] >= _RANK[Role(need)]
    except ValueError:
        return False

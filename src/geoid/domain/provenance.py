"""Provenance shaping — the ``geoid-prov/0.2`` contract.

Provenance is an append-only JSON object stored on the place. It records *who*
(``created_by``; ``null`` for anonymous contributions) and *which instance*
minted it (``originating_instance``; the federation seam) — nothing else.
Submitted GeoJSON ``properties`` are accepted (RFC 7946) but never persisted.

Authority is deliberately NOT here: a claim of authority *over* a place belongs
in the ``authority_assertion`` side table (append-only by design, not yet
trigger-enforced — a schema stub today, no behaviour yet), never as a mutable
column. This is what answers "anonymous yet authority-tracked" architecturally.
"""

from __future__ import annotations

from typing import Any

PROV_SCHEMA = "geoid-prov/0.2"


def build_provenance(*, created_by: str | None, originating_instance: str) -> dict[str, Any]:
    """Build the canonical provenance object stored on ``place.provenance``.

    Args:
        created_by: Subject of the authenticated principal, or ``None`` for anon.
        originating_instance: The minting instance id (federation seam).
    """
    return {
        "schema": PROV_SCHEMA,
        "created_by": created_by,
        "originating_instance": originating_instance,
    }

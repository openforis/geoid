"""Health probe response schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthStatus(BaseModel):
    """Health probe result — API process is up; ``db`` reports connectivity."""

    status: Literal["ok", "unavailable"] = Field(description="Overall service health.")
    db: Literal["up", "down"] = Field(description="Database connectivity probe result.")
    version: str = Field(description="GeoID service version.")
    detail: str | None = Field(
        default=None,
        description="Failure class name when db is down (never the message/DSN).",
    )

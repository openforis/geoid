"""CLI entrypoints — one image, two commands: ``web`` and ``migrate``.

geoid web      # run the API (uvicorn)
geoid migrate  # apply Alembic migrations to head (Cloud Run Job)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("geoid.cli")


def _alembic_ini() -> str:
    """Locate alembic.ini (Docker WORKDIR, repo root, or GEOID_ALEMBIC_INI)."""
    override = os.environ.get("GEOID_ALEMBIC_INI")
    if override:
        return override
    candidates = (
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "alembic.ini"


def run_web() -> None:
    import uvicorn

    # uvicorn's log_level configures only the uvicorn.* loggers; geoid.* records
    # propagate to a handler-less root where only WARNING+ leaks out via
    # logging.lastResort. Configure the root here so app INFO (OIDC rejections,
    # bootstrap) is actually emitted in the deployed image.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    # WEB_CONCURRENCY>1 spawns multiple worker processes (one event loop each);
    # on Cloud Run the simpler lever is concurrency + horizontal instances.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    uvicorn.run(
        "geoid.main:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        loop="uvloop",  # fail loud if the fast wheels are missing in the runtime image
        http="httptools",
        log_level="info",
    )


def run_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(_alembic_ini())
    command.upgrade(config, "head")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "web"
    if command == "web":
        run_web()
    elif command == "migrate":
        run_migrate()
    else:
        logger.error("unknown command %r; use 'web' or 'migrate'", command)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

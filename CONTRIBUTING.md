# Contributing to GeoID

GeoID is a Python service built with FastAPI, backed by PostgreSQL with PostGIS, and distributed
as a single Docker image. This guide covers the development workflow; how identity works is in the
README's *How identity works* section, and the design decisions behind it are the ADRs under
`docs/adr/`.

Run every command from the repository root (where `pyproject.toml` lives).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) — the project is uv-managed; always prefix commands with
  `uv run` and never invoke `pip`/`python` directly.
- Docker — for the local PostGIS stack and the testcontainers-based integration tests.
- Python ≥ 3.11.

## Setup

```bash
uv sync                 # create the venv + install (editable) from the lockfile
cp .env.example .env     # set GEOID_DATABASE_URL, GEOID_BASE_URL; GEOID_OIDC_* required outside development
```

## Run locally

Bring up PostGIS, apply the schema, then run the API with autoreload:

```bash
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn geoid.main:app --reload
```

Or run the whole stack (API + PostGIS) in one command:

```bash
docker compose up --build
```

Open `http://localhost:8000/docs` for Swagger and `http://localhost:8000/` for the OGC landing page.
If host port 5432 is taken, use `GEOID_DB_PORT=5433 docker compose up -d`.

## Branches & pull requests

Cut a feature branch off the default branch and open a pull request against it. Maintainers
release from the default branch.

## Commits

Use `<type>: <description>`, where `<type>` is one of `feat`, `fix`, `refactor`, `docs`, `test`,
`chore`, `perf`, `ci`. A `!` after the type marks a breaking change (e.g. `feat!: …`).

## Tests & coverage

```bash
uv run pytest -m unit          # pure unit tests, no Docker
uv run pytest -m integration   # ephemeral PostGIS via testcontainers (needs Docker)
```

Coverage floor is **80%**. You **must** set `COVERAGE_CORE=sysmon` when measuring coverage —
otherwise the legacy tracer under-reports the async ASGI handlers:

```bash
COVERAGE_CORE=sysmon uv run pytest --cov=geoid --cov-report=term-missing --cov-fail-under=80
```

## Lint / pre-commit

```bash
uv run --with pre-commit pre-commit run --all-files
```

The ruff config lives in `pyproject.toml` (`[tool.ruff]`); pre-commit reads it rather than
duplicating it. CI does **not** re-run `ruff check` — pre-commit owns lint.

## Migrations

The database is the source of truth, so schema changes ship as migrations:

```bash
uv run alembic revision -m "describe the change"   # name the file NNNN_snake_case.py
uv run alembic upgrade head                         # = uv run geoid migrate
```

Single-head is enforced in CI. **Never edit an already-applied migration — ship a new one.**

## Checks a PR must pass

```bash
uv run --with pre-commit pre-commit run --all-files                                 # lint / format
uv run alembic heads                                                                # exactly one head
COVERAGE_CORE=sysmon uv run pytest --cov=geoid --cov-report=term-missing --cov-fail-under=80
```

## Cutting a release

Maintainers only:

1. Bump `project.version` in `pyproject.toml` and run `uv lock` so the lockfile matches.
2. Prepend a `## [X.Y.Z] - DATE` section to `CHANGELOG.md` (Keep a Changelog format).
3. Commit both, tag `vX.Y.Z`, and create a GitHub Release with that changelog section as the notes.

`CHANGELOG.md` is the single source of truth; the GitHub Release body is derived from its version
section.

## Documentation

The tracked docs are the ADRs under `docs/adr/`. Keep `README.md` and `CHANGELOG.md` current when a
change alters behavior, config, or the schema.

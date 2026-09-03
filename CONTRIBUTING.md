# Contributing to GeoID

GeoID is one FastAPI service over one PostGIS schema, packaged as one Docker image. This guide
covers the development workflow; the architecture and load-bearing invariants live in `README.md`
and the ADRs under `docs/adr/`.

> **Project root is the nested `geoid/` directory** (where `pyproject.toml`, `src/`, and `tests/`
> live). Run every command from there.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) — the project is uv-managed; always prefix commands with
  `uv run` and never invoke `pip`/`python` directly.
- Docker — for the local PostGIS stack and the testcontainers-based integration tests.
- Python ≥ 3.11.

## Setup

```bash
uv sync                 # create the venv + install (editable) from the lockfile
cp .env.example .env     # set DATABASE_URL, BASE_URL, and OIDC config (required outside development)
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

## Branch model

- **Feature branches** are cut off `review`.
- **`review`** is the integration branch — pushing to it auto-deploys the review environment.
- **`main`** is the release source — a published GitHub Release deploys production. A plain push to
  `main` deploys nothing.

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

## CI gates

Three GitHub Actions workflows must be green before merge:

- **`pre-commit.yml`** — lint/format.
- **`ci.yml`** — the alembic single-head check plus `pytest --cov-fail-under=80`.
- **`deploy.yml`** — build → migrate → golden-vector canary → deploy → smoke.

## Cutting a release

On `review`:

1. Prepend a `## [X.Y.Z] - DATE` section to `CHANGELOG.md` (Keep a Changelog format).
2. Bump the version in `pyproject.toml`.
3. Commit those changes, then promote: `git push origin review:main`.
4. `gh release create vX.Y.Z --target main --notes-file <that changelog section>`.

Edit `CHANGELOG.md` **only on `review`** — that keeps the promote a fast-forward, and it is the
single source of truth (the GitHub Release body is derived from its version section).

## Documentation

The tracked docs are the ADRs under `docs/adr/`. Keep `README.md` and `CHANGELOG.md` current when a
change alters behavior, config, or the schema.

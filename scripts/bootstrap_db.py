#!/usr/bin/env python3
"""Bootstrap a fresh PostgreSQL instance (Cloud SQL or local) to GeoID app-ready.

One-time operational script, idempotent — safe to re-run. Run it from a synced
checkout, through the Cloud SQL Auth Proxy for Cloud SQL (it also works against
the local docker-compose PostGIS for rehearsal):

    GEOID_BOOTSTRAP_ADMIN_DSN='postgresql://postgres@127.0.0.1:5432/postgres' \\
    GEOID_DATABASE_URL='postgresql+asyncpg://geoid@127.0.0.1:5432/geoid' \\
    uv run python scripts/bootstrap_db.py [--dry-run] [--yes]

Steps (each idempotent):
    1. pre-flight    Settings check — catches the production dev-token trap before
                     anything mutates (`geoid migrate` would crash on it mid-run)
    2. role          CREATE ROLE ... LOGIN; an existing role's password is never
                     rotated unless --reset-app-password
    3. database      GRANT role TO admin (Cloud SQL's `postgres` is not a real
                     superuser and cannot CREATE DATABASE for a role it is not a
                     member of), then CREATE DATABASE ... OWNER role. Refuses to
                     adopt a pre-existing database with the wrong owner.
    4. harden        REVOKE CONNECT ON DATABASE ... FROM PUBLIC
    5. extensions    CREATE EXTENSION postgis, pgcrypto as the admin — on Cloud SQL
                     only `cloudsqlsuperuser` members may CREATE EXTENSION, so
                     migration 0001's IF NOT EXISTS then no-ops under the app role
    6. privileges    assert the owner has CONNECT + public-schema CREATE (PG15+
                     grants these via pg_database_owner when ownership is right)
    7. migrate       `geoid migrate` subprocess AS THE APP ROLE, so every table,
                     function and trigger is owned by it (--skip-migrate to skip)
    8. golden vectors  recompute the pinned identity corpus
                     (scripts/data/dedup_golden_vectors_v2.json) against the
                     DEPLOYED geoid_geom_hash_v2 + wrapper — recipe v2 is
                     engine-independent, so the check means "deployed function ≡
                     Python reference"; any drift joins the exit-3 path. Runs
                     AFTER migrate (the function must exist).
    9. seed          default catalog + reserved public collection (--skip-seed)
   10. verify        connect as the app role (doubles as a credential check):
                     alembic head, extensions, triggers, identity function, recipe
                     stamp (migration 0003's dedup_recipe_stamp), seed rows

Env:
    GEOID_BOOTSTRAP_ADMIN_DSN     required — admin (`postgres`) libpq DSN/URL to the
                                  maintenance database; password prompted if absent
    GEOID_DATABASE_URL            required — the canonical app URL exactly as
                                  production uses it; the role name, database name
                                  and app password are derived from it
    GEOID_BOOTSTRAP_APP_PASSWORD  optional — overrides the app password in the URL

Exit codes:
    0 ok · 1 step failed · 2 config/usage error · 3 completed with verification drift
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine.url import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_EXTENSIONS = ("postgis", "pgcrypto")
# Migration 0001's user-trigger inventory: 3 on place (after_insert,
# block_mutation, block_truncate) + 2×2 append-only guards on geoid_registry and
# change_log. (No BEFORE INSERT hash trigger: the dedup geom_hash lives on
# geoid_registry, written by the app's arbiter CTE — not a trigger.)
EXPECTED_USER_TRIGGERS = 7


class ConfigError(Exception):
    """Bad or missing configuration — exit 2, nothing was touched."""


class StepError(Exception):
    """A bootstrap step failed — exit 1; the message carries remediation hints."""


@dataclass(frozen=True)
class BootstrapConfig:
    admin_dsn: str
    admin_password: str | None
    app_url: str  # canonical postgresql+asyncpg URL with the effective password
    app_role: str
    app_db: str
    dry_run: bool
    skip_migrate: bool
    skip_seed: bool
    reset_app_password: bool
    assume_yes: bool

    @property
    def app_password(self) -> str:
        return make_url(self.app_url).password or ""


def _redact(dsn: str) -> str:
    """Mask the password in a URL or key=value DSN for printing."""
    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:
        return re.sub(r"(password\s*=\s*)\S+", r"\1***", dsn)


def _psycopg_dsn(app_url: str) -> str:
    """SQLAlchemy ``postgresql+asyncpg://`` URL → plain libpq/psycopg URL."""
    return make_url(app_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _derive_app_url(raw_url: str, password_override: str | None) -> tuple[str, str, str]:
    """(effective async app URL, role, database) derived from GEOID_DATABASE_URL."""
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise ConfigError(f"GEOID_DATABASE_URL is not a valid SQLAlchemy URL: {exc}") from exc
    if url.get_backend_name() != "postgresql":
        raise ConfigError(f"GEOID_DATABASE_URL must be a postgresql URL, got {url.drivername!r}")
    if not url.username or not url.database:
        raise ConfigError("GEOID_DATABASE_URL must carry a role name and a database name")
    url = url.set(drivername="postgresql+asyncpg")
    if password_override:
        url = url.set(password=password_override)
    return url.render_as_string(hide_password=False), url.username, url.database


def _resolve_app_password(url_password: str | None, override: str | None) -> str:
    if override:
        return override
    if url_password:
        return url_password
    if sys.stdin.isatty():
        prompted = getpass.getpass("app role password (= GEOID_BOOTSTRAP_APP_PASSWORD): ")
        if prompted:
            return prompted
    raise ConfigError(
        "no app password available — put it in GEOID_DATABASE_URL or set "
        "GEOID_BOOTSTRAP_APP_PASSWORD (it is needed to create the role and to verify)"
    )


def _resolve_admin_password(admin_dsn: str) -> str | None:
    """Prompt for the admin password only when the DSN and libpq env lack one."""
    try:
        params = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    except psycopg.ProgrammingError as exc:
        raise ConfigError(f"GEOID_BOOTSTRAP_ADMIN_DSN is not a valid DSN: {exc}") from exc
    if params.get("password") or os.environ.get("PGPASSWORD"):
        return None
    if sys.stdin.isatty():
        prompted = getpass.getpass(
            f"admin password for {_redact(admin_dsn)} (empty = .pgpass/trust): "
        )
        return prompted or None
    return None


def _load_config(argv: list[str] | None = None) -> BootstrapConfig:
    parser = argparse.ArgumentParser(
        description="Bootstrap a fresh PostgreSQL instance to GeoID app-ready (idempotent)."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the statements that would run; mutate nothing"
    )
    parser.add_argument(
        "--skip-migrate",
        action="store_true",
        help="skip `geoid migrate` (the Cloud Run job will run it)",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="skip seeding the default catalog + public collection",
    )
    parser.add_argument(
        "--reset-app-password",
        action="store_true",
        help="rotate an existing role's password to the configured one",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    admin_dsn = os.environ.get("GEOID_BOOTSTRAP_ADMIN_DSN")
    raw_app_url = os.environ.get("GEOID_DATABASE_URL")
    if not admin_dsn:
        raise ConfigError(
            "GEOID_BOOTSTRAP_ADMIN_DSN is required "
            "(admin DSN to the maintenance database, e.g. .../postgres)"
        )
    if not raw_app_url:
        raise ConfigError("GEOID_DATABASE_URL is required (the canonical app URL)")

    _derive_app_url(raw_app_url, None)  # validate the URL before touching its parts
    app_password = _resolve_app_password(
        make_url(raw_app_url).password, os.environ.get("GEOID_BOOTSTRAP_APP_PASSWORD")
    )
    app_url, app_role, app_db = _derive_app_url(raw_app_url, app_password)
    return BootstrapConfig(
        admin_dsn=admin_dsn,
        admin_password=_resolve_admin_password(admin_dsn),
        app_url=app_url,
        app_role=app_role,
        app_db=app_db,
        dry_run=args.dry_run,
        skip_migrate=args.skip_migrate,
        skip_seed=args.skip_seed,
        reset_app_password=args.reset_app_password,
        assume_yes=args.yes,
    )


def _preflight_settings(cfg: BootstrapConfig):
    """Catch the production dev-token trap before anything mutates.

    The seed step constructs full Settings; the migrate subprocess needs only
    DatabaseSettings (no token). Failing here keeps the failure clean.
    """
    from pydantic import ValidationError

    from geoid.config import Settings

    env_file = REPO_ROOT / ".env"
    try:
        if env_file.is_file():  # what the migrate subprocess (cwd=REPO_ROOT) will see
            Settings(database_url=cfg.app_url, _env_file=str(env_file))
        return Settings(database_url=cfg.app_url, _env_file=None)  # what the seed step uses
    except ValidationError as exc:
        raise ConfigError(
            f"settings pre-flight failed:\n{exc}\n"
            "  Hint: GEOID_ENVIRONMENT != development requires GEOID_OIDC_ISSUER and "
            "GEOID_OIDC_JWKS_URL in the environment before bootstrapping."
        ) from exc


def _connect(cfg: BootstrapConfig, *, dbname: str | None = None) -> psycopg.Connection:
    # autocommit: CREATE DATABASE / CREATE ROLE cannot run inside a transaction block.
    kwargs: dict[str, str] = {}
    if cfg.admin_password:
        kwargs["password"] = cfg.admin_password
    if dbname:
        kwargs["dbname"] = dbname
    try:
        return psycopg.connect(cfg.admin_dsn, autocommit=True, **kwargs)
    except psycopg.OperationalError as exc:
        target = dbname or "the maintenance database"
        raise StepError(
            f"cannot connect as admin to {target}: {exc}\n"
            "  Is the Cloud SQL Auth Proxy running, and are the DSN host/port correct?"
        ) from exc


def _run(
    conn: psycopg.Connection,
    cfg: BootstrapConfig,
    statement: sql.Composed,
    display: str | None = None,
) -> None:
    text = display or statement.as_string(conn)
    if cfg.dry_run:
        print(f"  [dry-run] {text}")
    else:
        conn.execute(statement)


def ensure_role(conn: psycopg.Connection, cfg: BootstrapConfig) -> bool:
    """Create the app role if missing. Returns True when it already existed."""
    print(f"→ role {cfg.app_role!r}")
    existed = (
        conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (cfg.app_role,)).fetchone()
        is not None
    )
    if not existed:
        statement = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(cfg.app_role), sql.Literal(cfg.app_password)
        )
        _run(
            conn, cfg, statement, display=f"CREATE ROLE {cfg.app_role} LOGIN PASSWORD '<redacted>'"
        )
        if not cfg.dry_run:
            print("  ✓ created")
    elif cfg.reset_app_password:
        statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(cfg.app_role), sql.Literal(cfg.app_password)
        )
        _run(conn, cfg, statement, display=f"ALTER ROLE {cfg.app_role} PASSWORD '<redacted>'")
        if not cfg.dry_run:
            print("  ✓ exists — password reset (--reset-app-password)")
    else:
        print("  ✓ exists (password untouched; pass --reset-app-password to rotate)")
    return existed


def ensure_database(conn: psycopg.Connection, cfg: BootstrapConfig) -> bool:
    """Create the app database owned by the app role. Returns True when it existed."""
    print(f"→ database {cfg.app_db!r}")
    admin_user = conn.execute("SELECT current_user").fetchone()[0]
    if admin_user != cfg.app_role:
        # Cloud SQL's `postgres` is not a real superuser: it must be a member of the
        # target role to create a database owned by it. Re-granting is a no-op.
        _run(
            conn,
            cfg,
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(cfg.app_role), sql.Identifier(admin_user)
            ),
        )
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (cfg.app_db,)
    ).fetchone()
    if row is None:
        _run(
            conn,
            cfg,
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(cfg.app_db), sql.Identifier(cfg.app_role)
            ),
        )
        if not cfg.dry_run:
            print(f"  ✓ created, owner {cfg.app_role!r}")
        return False
    owner = row[0]
    if owner != cfg.app_role:
        raise StepError(
            f"database {cfg.app_db!r} exists but is owned by {owner!r}, not "
            f"{cfg.app_role!r} — refusing to adopt a database this script did not create.\n"
            f"  If taking it over is intended, run as admin:\n"
            f'    ALTER DATABASE "{cfg.app_db}" OWNER TO "{cfg.app_role}";\n'
            f"  then re-run this script."
        )
    print(f"  ✓ exists, owner {cfg.app_role!r}")
    return True


def harden_database(conn: psycopg.Connection, cfg: BootstrapConfig) -> None:
    print("→ harden")
    # Without this, every console-created Cloud SQL user (all cloudsqlsuperuser
    # members) could connect via the default PUBLIC grant.
    _run(
        conn,
        cfg,
        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(cfg.app_db)),
    )
    if not cfg.dry_run:
        print("  ✓ CONNECT revoked from PUBLIC (owner keeps it implicitly)")


def ensure_extensions(conn: psycopg.Connection, cfg: BootstrapConfig) -> None:
    print("→ extensions (as admin — Cloud SQL restricts CREATE EXTENSION to cloudsqlsuperuser)")
    for extension in REQUIRED_EXTENSIONS:
        _run(
            conn,
            cfg,
            sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(extension)),
        )
    if cfg.dry_run:
        return
    rows = conn.execute(
        "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s) ORDER BY extname",
        (list(REQUIRED_EXTENSIONS),),
    ).fetchall()
    for name, version in rows:
        print(f"  ✓ {name} {version}")
    missing = set(REQUIRED_EXTENSIONS) - {name for name, _ in rows}
    if missing:
        raise StepError(f"extensions failed to install: {sorted(missing)}")


def _load_dedup_vectors():
    """Load scripts/dedup_vectors.py as a module (scripts/ is not a package)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "dedup_vectors.py"
    spec = importlib.util.spec_from_file_location("dedup_vectors", path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["dedup_vectors"] = module
    spec.loader.exec_module(module)
    return module


def check_hash_vectors(conn: psycopg.Connection) -> list[str]:
    """Recompute the pinned golden-vector corpus against the DEPLOYED identity
    recipe (``geoid_geom_hash_v2`` + the ``geoid_geom_hash_default`` wrapper).
    Recipe v2 is engine-independent, so any drift means the deployed SQL does not
    match the Python reference — never an engine-version artifact. Runs AFTER
    migrate (the function must exist)."""
    print("→ identity golden vectors (deployed function ≡ Python reference)")
    vectors = _load_dedup_vectors()
    run_sql = vectors._psycopg_run_sql(conn)
    try:
        vectors.ensure_v2_deployed(run_sql)
        fixture = vectors.load_fixture()
        report = vectors.check_vectors(run_sql, fixture)
    except vectors.StepError as exc:
        raise StepError(str(exc)) from exc
    problems = [
        f"golden vector {failure.name!r} drifted — expected {failure.expected}, got "
        f"{failure.actual}; the deployed SQL recipe does not match the Python reference "
        "(migration 0008 vs domain/geometry_identity drift) — do NOT load data"
        for failure in report.failures
    ]
    for problem in problems:
        print(f"  ⚠ {problem}")
    if not problems:
        print(
            f"  ✓ {report.passed}/{len(fixture['vectors'])} golden vectors match "
            f"({vectors.FIXTURE_PATH.name})"
        )
    return problems


def check_owner_privileges(conn: psycopg.Connection, cfg: BootstrapConfig) -> None:
    print("→ owner privileges")
    connect_ok, create_ok = conn.execute(
        "SELECT has_database_privilege(%(role)s, %(db)s, 'CONNECT'), "
        "has_schema_privilege(%(role)s, 'public', 'CREATE')",
        {"role": cfg.app_role, "db": cfg.app_db},
    ).fetchone()
    if not (connect_ok and create_ok):
        raise StepError(
            f"role {cfg.app_role!r} lacks privileges on {cfg.app_db!r} "
            f"(CONNECT={connect_ok}, public-schema CREATE={create_ok}).\n"
            "  With correct ownership PG15+ grants both via pg_database_owner — the "
            "ownership is probably wrong; fix that instead of papering over with GRANTs."
        )
    print("  ✓ CONNECT + public-schema CREATE (held via ownership)")


def run_migrations(cfg: BootstrapConfig) -> None:
    print("→ migrations (`geoid migrate` as the app role, so objects are owned by it)")
    # Subprocess, not the in-process alembic API: byte-parity with the production
    # Cloud Run migrate job, no lru_cache'd get_settings() bleed-through, and
    # env.py calls asyncio.run() which must own the event loop.
    env = os.environ | {"GEOID_DATABASE_URL": cfg.app_url}
    result = subprocess.run([sys.executable, "-m", "geoid.cli", "migrate"], env=env, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise StepError(f"`geoid migrate` exited {result.returncode} — see alembic output above")
    print("  ✓ migrated to head")


async def _seed_async(cfg: BootstrapConfig, settings) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from geoid.services.bootstrap import ensure_public_collection

    # Throwaway engine: geoid.db.get_engine() is a settings-cache-bound module global.
    engine = create_async_engine(cfg.app_url, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            await ensure_public_collection(session, settings)
            await session.commit()  # ensure_public_collection only flushes
    finally:
        await engine.dispose()


def seed(cfg: BootstrapConfig, settings) -> None:
    print("→ seed (default catalog + reserved public collection)")
    asyncio.run(_seed_async(cfg, settings))
    print(f"  ✓ collection {settings.public_collection!r} ensured")


def _alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from geoid import cli

    config = Config(cli._alembic_ini())
    # script_location in alembic.ini is cwd-relative; pin it to this checkout.
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _scalar(conn: psycopg.Connection, query: str, params=()) -> object:
    try:
        row = conn.execute(query, params).fetchone()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    return row[0] if row else None


def verify(cfg: BootstrapConfig) -> list[str]:
    """Read back the final state as the app role (doubles as a credential check)."""
    print("→ verify (connecting as the app role)")
    try:
        conn = psycopg.connect(_psycopg_dsn(cfg.app_url))
    except psycopg.OperationalError as exc:
        raise StepError(
            f"cannot connect as {cfg.app_role!r}: {exc}\n"
            "  Wrong app password? A pre-existing role keeps its old password unless "
            "--reset-app-password is passed."
        ) from exc
    with conn:
        head = _alembic_head()
        version = _scalar(conn, "SELECT version_num FROM alembic_version")
        extensions = dict(
            conn.execute(
                "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s)",
                (list(REQUIRED_EXTENSIONS),),
            ).fetchall()
        )
        triggers = _scalar(
            conn,
            """
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname = 'public'
        """,
        )
        hash_fn = _scalar(conn, "SELECT count(*) FROM pg_proc WHERE proname = 'geoid_geom_hash_v2'")
        recipe_version = _scalar(
            conn, "SELECT recipe_version FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
        )
        catalogs = _scalar(conn, "SELECT count(*) FROM catalog")
        collections = _scalar(conn, "SELECT count(*) FROM collection")
        create_ok = _scalar(conn, "SELECT has_schema_privilege('public', 'CREATE')")

    checks = (
        (
            "alembic version",
            version or "<absent>",
            version == head,
            f"alembic_version is {version!r}, head is {head!r} — re-run without --skip-migrate",
        ),
        (
            "postgis",
            extensions.get("postgis", "<absent>"),
            "postgis" in extensions,
            "postgis extension missing",
        ),
        (
            "pgcrypto",
            extensions.get("pgcrypto", "<absent>"),
            "pgcrypto" in extensions,
            "pgcrypto extension missing",
        ),
        (
            "user triggers",
            f"{triggers} (expect >= {EXPECTED_USER_TRIGGERS})",
            (triggers or 0) >= EXPECTED_USER_TRIGGERS,
            f"only {triggers} user triggers — migration 0001 creates {EXPECTED_USER_TRIGGERS}",
        ),
        (
            "geoid_geom_hash_v2()",
            "present" if hash_fn else "missing",
            bool(hash_fn),
            "identity function geoid_geom_hash_v2 is missing — migrate to 0008+",
        ),
        (
            "recipe stamp",
            recipe_version or "<absent>",
            recipe_version == "v2",
            f"latest dedup_recipe_stamp.recipe_version is {recipe_version!r}, expected 'v2' "
            "— migrate to 0008 (the identity-recipe-v2 migration; it refuses a non-empty "
            "registry — see the re-mint runbook, local-scripts/docs/DEPLOYMENT.md §15)",
        ),
        (
            "catalogs / collections",
            f"{catalogs} / {collections}",
            (catalogs or 0) >= 1 and (collections or 0) >= 1,
            "seed rows missing — re-run without --skip-seed",
        ),
        (
            "schema CREATE privilege",
            "yes" if create_ok else "no",
            bool(create_ok),
            f"role {cfg.app_role!r} cannot CREATE in schema public",
        ),
    )
    for label, value, ok, _ in checks:
        print(f"  {'✓' if ok else '✗'} {label:<26} {value}")
    return [problem for _, _, ok, problem in checks if not ok]


def _print_plan(cfg: BootstrapConfig) -> None:
    print(f"GeoID bootstrap ({'DRY-RUN' if cfg.dry_run else 'live'})")
    print(f"  admin DSN : {_redact(cfg.admin_dsn)}")
    print(f"  app URL   : {_redact(cfg.app_url)}")
    print(f"  role / db : {cfg.app_role} / {cfg.app_db}")
    skips = [flag for flag, on in (("migrate", cfg.skip_migrate), ("seed", cfg.skip_seed)) if on]
    if skips:
        print(f"  skipping  : {', '.join(skips)}")
    print()


def _confirmed(cfg: BootstrapConfig) -> bool:
    if cfg.assume_yes or cfg.dry_run:
        return True
    return input("proceed? [y/N] ").strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = _load_config(argv)
        settings = _preflight_settings(cfg)
    except ConfigError as exc:
        print(f"✗ {exc}")
        return 2

    _print_plan(cfg)
    if not _confirmed(cfg):
        print("aborted")
        return 2

    try:
        with _connect(cfg) as conn:
            role_existed = ensure_role(conn, cfg)
            db_existed = ensure_database(conn, cfg)
            harden_database(conn, cfg)

        if cfg.dry_run and not db_existed:
            print(
                f"\n[dry-run] database {cfg.app_db!r} does not exist yet — extensions, "
                "parity check, migrations, seed and verify would follow its creation"
            )
            print("✓ dry-run complete (nothing mutated)")
            return 0

        drift: list[str] = []
        with _connect(cfg, dbname=cfg.app_db) as conn:
            ensure_extensions(conn, cfg)
            if role_existed or not cfg.dry_run:
                check_owner_privileges(conn, cfg)

        if cfg.dry_run:
            print(
                f"\n[dry-run] would then run `geoid migrate` (as {cfg.app_role!r}), "
                "check the identity golden vectors against the deployed recipe, "
                "seed the public collection, and verify"
            )
            print("✓ dry-run complete (nothing mutated)")
            return 0

        if cfg.skip_migrate:
            print("→ migrations skipped (--skip-migrate)")
        else:
            run_migrations(cfg)
        # The vectors check validates the DEPLOYED geoid_geom_hash_v2 (recipe v2 is
        # engine-independent), so it can only run once migrate has created it.
        if cfg.skip_migrate:
            print("→ identity golden vectors skipped (--skip-migrate: function not deployed)")
        else:
            with _connect(cfg, dbname=cfg.app_db) as conn:
                drift += check_hash_vectors(conn)
        if cfg.skip_seed:
            print("→ seed skipped (--skip-seed)")
        else:
            seed(cfg, settings)
        drift += verify(cfg)
    except StepError as exc:
        print(f"✗ {exc}")
        return 1

    if drift:
        print("\n⚠ bootstrap completed with verification drift:")
        for problem in drift:
            print(f"  - {problem}")
        return 3
    print("\n✓ bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

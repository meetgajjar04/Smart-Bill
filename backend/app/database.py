"""Database engine, session factory and ordered schema migrations.

SQLite is still supported (tests and a zero-setup demo) and is created from the
ORM metadata only. The application's normal database is MySQL/MariaDB under
XAMPP: its schema is the authoritative, versioned SQL file in
backend/db/mysql_schema.sql. On MySQL the file must exist and every version in
it is applied in order, once, and recorded with a timestamp. A version that is
present in the database but unknown to the runner is a hard startup error; a
failed migration is never recorded as applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_url = make_url(settings.database_url)
_backend = _url.get_backend_name()
is_sqlite = _backend == "sqlite"
is_mysql = _backend in ("mysql", "mariadb")

_args = {}
if is_sqlite:
    _args["connect_args"] = {"check_same_thread": False}
elif is_mysql:
    _args.update(
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,          # stay below MariaDB's default 8-hour wait_timeout
        connect_args={
            "charset": "utf8mb4",
            "connect_timeout": 10,
            # Store and read DATETIME values as UTC, regardless of the PC's clock.
            "init_command": "SET time_zone = '+00:00'",
        },
    )

engine = create_engine(settings.database_url, pool_pre_ping=True, **_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_healthy() -> bool:
    """True when the engine can actually reach the database."""
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SQL parsing (one implementation shared by the app and the maintenance scripts)
# ---------------------------------------------------------------------------
def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script on semicolons that are actually statement terminators.

    Handles ``-- ...`` and ``# ...`` line comments, ``/* ... */`` block comments,
    single/double quoted strings (both doubled quotes and backslash escapes) and
    backtick-quoted identifiers. A naive ``sql.split(';')`` corrupts statements
    containing a semicolon inside a string or comment.
    """
    statements: list[str] = []
    buffer: list[str] = []
    state = "normal"           # normal | single | double | backtick | line | block
    i, n = 0, len(sql)

    def finish() -> None:
        statement = "".join(buffer).strip()
        if statement:
            statements.append(statement)
        buffer.clear()

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if state == "normal":
            # MySQL only treats "-- " (dash dash followed by whitespace/EOL) as a comment.
            if ch == "-" and nxt == "-" and (i + 2 >= n or sql[i + 2].isspace()):
                state = "line"
                i += 2
                continue
            if ch == "#":
                state = "line"
                i += 1
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if ch in ("'", '"', "`"):
                state = {"'": "single", '"': "double", "`": "backtick"}[ch]
                buffer.append(ch)
                i += 1
                continue
            if ch == ";":
                finish()
                i += 1
                continue
            buffer.append(ch)
            i += 1
            continue

        if state == "line":
            if ch in "\r\n":
                buffer.append("\n")  # keep a separator so tokens do not join
                state = "normal"
            i += 1
            continue

        if state == "block":
            if ch == "*" and nxt == "/":
                state = "normal"
                i += 2
                continue
            if ch in "\r\n":
                buffer.append("\n")
            i += 1
            continue

        # Inside a quoted string or identifier.
        quote = {"single": "'", "double": '"', "backtick": "`"}[state]
        if ch == "\\" and state != "backtick" and i + 1 < n:
            buffer.append(ch)
            buffer.append(sql[i + 1])
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:   # SQL-escaped quote ('It''s')
                buffer.append(ch)
                buffer.append(ch)
                i += 2
                continue
            buffer.append(ch)
            state = "normal"
            i += 1
            continue
        buffer.append(ch)
        i += 1

    if state in ("single", "double", "backtick", "block"):
        raise ValueError(f"Unterminated SQL {state} literal/comment at end of script")
    finish()
    return statements


# ---------------------------------------------------------------------------
# Versioned migrations
# ---------------------------------------------------------------------------
SCHEMA_FILE = Path(__file__).resolve().parents[1] / "db" / "mysql_schema.sql"
MIGRATION_TABLE = "schema_migrations"
BASE_TABLES = ("users", "household_profiles", "bills", "alerts", "ai_insights")
MARKER = re.compile(r"^\s*--\s*@migration:\s*(\d{3})\s*\|\s*(.+?)\s*$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchemaMigrationError(RuntimeError):
    """A schema version is unknown, missing, or a migration failed."""


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    statements: tuple[str, ...]


def parse_migrations(sql: str) -> dict[str, Migration]:
    """Read ``-- @migration: NNN | description`` blocks from the schema file."""
    migrations: dict[str, Migration] = {}
    version: str | None = None
    description = ""
    body: list[str] = []

    def finalize() -> None:
        if version is None:
            return
        statements = tuple(split_sql_statements("\n".join(body)))
        if version in migrations:
            raise SchemaMigrationError(f"Duplicate migration version {version}")
        migrations[version] = Migration(version, description, statements)

    for line in sql.splitlines():
        match = MARKER.match(line)
        if match:
            finalize()
            version, description = match.group(1), match.group(2).strip()
            body = []
        elif version is not None:
            body.append(line)
    finalize()

    if not migrations:
        raise SchemaMigrationError(f"No -- @migration markers found in {SCHEMA_FILE}")
    return {v: migrations[v] for v in sorted(migrations)}


def _table_name(table: str) -> str:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"Unsafe migration table name: {table!r}")
    return table


def _create_migrations_table(conn, table: str) -> None:
    table = _table_name(table)
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql(
            f"CREATE TABLE IF NOT EXISTS `{table}` ("
            "version VARCHAR(20) NOT NULL PRIMARY KEY, "
            "description VARCHAR(200) NOT NULL, "
            "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    else:
        conn.exec_driver_sql(
            f"CREATE TABLE IF NOT EXISTS `{table}` ("
            "version VARCHAR(20) NOT NULL, "
            "description VARCHAR(200) NOT NULL, "
            "applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), "
            f"PRIMARY KEY (version)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )


def applied_versions(conn, table: str = MIGRATION_TABLE) -> set[str]:
    rows = conn.exec_driver_sql(f"SELECT version FROM `{_table_name(table)}`").fetchall()
    return {str(row[0]) for row in rows}


def _record(conn, migration: Migration, table: str) -> None:
    conn.execute(
        text(f"INSERT INTO `{_table_name(table)}` (version, description, applied_at) "
             "VALUES (:version, :description, CURRENT_TIMESTAMP)"),
        {"version": migration.version, "description": migration.description[:200]},
    )


def _check_version_chain(known: dict[str, Migration], applied: set[str], table: str) -> None:
    unknown = sorted(applied - set(known))
    if unknown:
        raise SchemaMigrationError(
            f"Unknown schema version(s) {', '.join(unknown)} in `{table}`; this build knows "
            f"{', '.join(known) or '(none)'}. Refusing to start: restore a matching backup or "
            "upgrade the application before running against this database."
        )
    if applied:
        latest = max(applied)
        missing = [v for v in known if v < latest and v not in applied]
        if missing:
            raise SchemaMigrationError(
                f"Schema versions {', '.join(missing)} are missing before recorded version {latest} "
                f"in `{table}`. Refusing to skip or downgrade a migration."
            )


def apply_migrations(
    engine: Engine,
    migrations: dict[str, Migration] | None = None,
    *,
    sql_text: str | None = None,
    table: str = MIGRATION_TABLE,
    baseline_if_tables_exist: bool = True,
    skip_existing_objects: bool = True,
) -> list[str]:
    """Apply every pending version in order and return the versions applied.

    ``baseline_if_tables_exist`` records version 001 without re-running its
    CREATE TABLE statements when the base tables are already present (legacy
    databases had a decorative migrations table). ``skip_existing_objects``
    makes the 002 ALTERs resumable if an earlier attempt added only some of the
    objects. Neither option can hide an unknown version or a failed statement.
    """
    if migrations is None:
        if sql_text is None:
            if not SCHEMA_FILE.exists():
                raise SchemaMigrationError(f"Schema file is missing: {SCHEMA_FILE}")
            sql_text = SCHEMA_FILE.read_text(encoding="utf-8")
        migrations = parse_migrations(sql_text)

    applied: list[str] = []
    with engine.connect() as conn:
        ledger_exists = _table_name(table) in inspect(conn).get_table_names()
        done = applied_versions(conn, table) if ledger_exists else set()
    if not ledger_exists:
        with engine.begin() as conn:
            _create_migrations_table(conn, table)
    _check_version_chain(migrations, done, table)

    # A legacy install could have a decorative ledger row without the actual
    # objects. Treat that drift as a broken schema, not as a finished migration.
    if engine.dialect.name != "sqlite":
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            if "002" in done and "002" in migrations and "alerts" in tables:
                columns = {c["name"] for c in inspector.get_columns("alerts")}
                indexes = inspector.get_indexes("alerts")
                has_key = any(idx.get("name") == "uq_alerts_user_dedupe" for idx in indexes)
                if "dedupe_key" not in columns or not has_key:
                    raise SchemaMigrationError(
                        "Schema version 002 is recorded but alerts.dedupe_key / "
                        "uq_alerts_user_dedupe is missing. Restore a matching backup or "
                        "re-run scripts.setup_mysql with --replace; refusing to start."
                    )
            if "003" in done and "003" in migrations and "users" in tables:
                columns = {c["name"] for c in inspector.get_columns("users")}
                if not {"token_version", "is_active"} <= columns:
                    raise SchemaMigrationError(
                        "Schema version 003 is recorded but users.token_version / "
                        "users.is_active is missing. Restore a matching backup or "
                        "re-run scripts.setup_mysql with --replace; refusing to start."
                    )
            if "004" in done and "004" in migrations and "rate_limit_hits" not in tables:
                raise SchemaMigrationError(
                    "Schema version 004 is recorded but rate_limit_hits is missing. "
                    "Restore a matching backup or re-run scripts.setup_mysql with "
                    "--replace; refusing to start."
                )
            if "005" in done and "005" in migrations and "rate_limit_locks" not in tables:
                raise SchemaMigrationError(
                    "Schema version 005 is recorded but rate_limit_locks is missing. "
                    "Restore a matching backup or re-run scripts.setup_mysql with "
                    "--replace; refusing to start."
                )

    # One transaction per version: a failure cannot mark that version as applied
    # and cannot undo migrations that already succeeded.
    for version, migration in migrations.items():
        if version in done:
            continue
        with engine.begin() as conn:
            existing = set(inspect(conn).get_table_names())
            if baseline_if_tables_exist and version == min(migrations) and set(BASE_TABLES) <= existing:
                _record(conn, migration, table)   # legacy DB: base tables already there
                applied.append(version)
                continue

            for statement in migration.statements:
                if skip_existing_objects and version == "002" and "alerts" in existing:
                    if "ADD COLUMN dedupe_key" in statement:
                        columns = {c["name"] for c in inspect(conn).get_columns("alerts")}
                        if "dedupe_key" in columns:
                            continue
                    if "uq_alerts_user_dedupe" in statement:
                        indexes = inspect(conn).get_indexes("alerts")
                        if any(idx.get("name") == "uq_alerts_user_dedupe" for idx in indexes):
                            continue
                if skip_existing_objects and version == "005" and "rate_limit_locks" in existing:
                    continue
                if skip_existing_objects and version == "003" and "users" in existing:
                    columns = {c["name"] for c in inspect(conn).get_columns("users")}
                    if "ADD COLUMN token_version" in statement and "token_version" in columns:
                        continue
                    if "ADD COLUMN is_active" in statement and "is_active" in columns:
                        continue
                try:
                    conn.exec_driver_sql(statement)
                except Exception as exc:
                    raise SchemaMigrationError(
                        f"Migration {version} ({migration.description}) failed: {exc}"
                    ) from exc
            # The canonical SQL file records its own versions with a timestamp;
            # custom/test migrations that do not are recorded here instead.
            if version not in applied_versions(conn, table):
                _record(conn, migration, table)
        applied.append(version)
    return applied


def init_db() -> None:
    """Create or migrate the schema; never silently fall back to a different engine.

    SQLite (tests/demo) uses the ORM metadata and stays free of MySQL-only ALTER
    syntax. MySQL/MariaDB requires the versioned SQL file: a missing file, an
    unknown recorded version or a failed migration stops startup with a clear
    error naming the version.
    """
    if not is_mysql:
        Base.metadata.create_all(engine)
        return
    if not SCHEMA_FILE.exists():
        raise SchemaMigrationError(f"Schema file is missing: {SCHEMA_FILE}. Refusing to start with an unknown schema.")
    apply_migrations(engine)

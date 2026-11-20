"""Regression tests for the schema handoff: versioned migrations and SQL parsing.

Everything runs on the throwaway SQLite URL pinned by conftest. No MySQL server
(or the live `smartbill` database) is contacted; migration steps are synthetic
and use their own table names so the rest of the suite is unaffected.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import database as db
from app.database import (
    Migration, SCHEMA_FILE, SchemaMigrationError,
    apply_migrations, parse_migrations, split_sql_statements,
)

MIGRATION_TABLE = "schema_migrations_handoff_test"
STEPS_TABLE = "handoff_steps_test"


@pytest.fixture
def handoff_tables():
    engine = db.engine

    def clean():
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{STEPS_TABLE}`")
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{MIGRATION_TABLE}`")

    clean()
    yield engine
    clean()


def fake_migrations() -> dict[str, Migration]:
    return {
        # Explicit ids keep this synthetic schema portable: SQLite's AUTOINCREMENT
        # is not valid MySQL/MariaDB syntax and neither engine needs it here.
        "001": Migration("001", "first step", (
            f"CREATE TABLE `{STEPS_TABLE}` (id INTEGER NOT NULL PRIMARY KEY, step VARCHAR(10) NOT NULL)",
            f"INSERT INTO `{STEPS_TABLE}` (id, step) VALUES (1, '001')",
        )),
        "002": Migration("002", "second step", (
            f"INSERT INTO `{STEPS_TABLE}` (id, step) VALUES (2, '002')",
        )),
    }


def versions(engine) -> set[str]:
    with engine.connect() as conn:
        return {str(row[0]) for row in conn.exec_driver_sql(f"SELECT version FROM `{MIGRATION_TABLE}`")}


def test_split_sql_statements_handles_comments_and_string_semicolons():
    sql = r"""
    -- a comment with a ; and a quote '
    SELECT 'alpha;beta' AS a;   # hash comment ;
    SELECT "gamma;delta" AS b;  /* block comment;
       spanning lines with ; */
    SELECT `odd;column` FROM t;
    SELECT 'It''s; fine' AS c;
    SELECT 'backslash\;still;one' AS d;
    """
    statements = split_sql_statements(sql)
    assert len(statements) == 5
    assert "SELECT 'alpha;beta' AS a" in statements[0]
    assert "SELECT \"gamma;delta\" AS b" in statements[1]
    assert "SELECT `odd;column` FROM t" in statements[2]
    assert "'It''s; fine'" in statements[3]
    assert "backslash\\;still;one" in statements[4]


def test_canonical_schema_file_has_ordered_002_ddl():
    migrations = parse_migrations(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert list(migrations) == ["001", "002", "003", "004", "005"]
    assert migrations["001"].description.startswith("initial schema")
    assert "dedupe_key" in migrations["002"].description
    assert "token_version" in migrations["003"].description
    assert "rate_limit_hits" in migrations["004"].description
    sql_004 = "\n".join(migrations["004"].statements).lower()
    assert "create table if not exists rate_limit_hits" in sql_004
    assert "char(64)" in sql_004  # bucket is a fixed-size SHA-256 digest
    assert "ix_rate_limit_hits_bucket_time" in sql_004
    assert "('004'," in sql_004
    assert "rate_limit_locks" in migrations["005"].description
    sql_005 = "\n".join(migrations["005"].statements).lower()
    assert "create table if not exists rate_limit_locks" in sql_005
    assert "primary key (bucket)" in sql_005
    assert "('005'," in sql_005
    sql_003 = "\n".join(migrations["003"].statements).lower()
    assert "alter table users add column token_version int unsigned not null default 0" in sql_003
    assert "alter table users add column is_active tinyint(1) not null default 1" in sql_003
    assert "('003'," in sql_003
    sql_002 = "\n".join(migrations["002"].statements).lower()
    assert "alter table alerts add column dedupe_key varchar(80) null" in sql_002
    assert "delete a from alerts as a" in sql_002
    assert "d.id < a.id" in sql_002
    assert "add unique key uq_alerts_user_dedupe (user_id, dedupe_key)" in sql_002
    assert sql_002.index("delete a from alerts") < sql_002.index("add unique key")
    assert "('002'," in sql_002  # normalized ledger insert with a version


def test_apply_migrations_runs_in_order_records_descriptions_and_is_idempotent(handoff_tables):
    engine = handoff_tables
    applied = apply_migrations(
        engine, fake_migrations(), table=MIGRATION_TABLE,
        baseline_if_tables_exist=False, skip_existing_objects=False,
    )
    assert applied == ["001", "002"]

    with engine.connect() as conn:
        steps = [r[0] for r in conn.exec_driver_sql(f"SELECT step FROM `{STEPS_TABLE}` ORDER BY id")]
        rows = conn.execute(text(
            f"SELECT version, description, applied_at FROM `{MIGRATION_TABLE}` ORDER BY version"
        )).all()
    assert steps == ["001", "002"]
    assert [(r[0], r[1]) for r in rows] == [("001", "first step"), ("002", "second step")]
    assert all(r[2] is not None for r in rows)

    assert apply_migrations(
        engine, fake_migrations(), table=MIGRATION_TABLE,
        baseline_if_tables_exist=False, skip_existing_objects=False,
    ) == []
    assert versions(engine) == {"001", "002"}


def test_unknown_schema_version_refuses_startup(handoff_tables):
    engine = handoff_tables
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE TABLE `{MIGRATION_TABLE}` (version VARCHAR(20) PRIMARY KEY, "
            "description VARCHAR(200) NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(text(
            f"INSERT INTO `{MIGRATION_TABLE}` (version, description, applied_at) "
            "VALUES ('999', 'from a newer build', CURRENT_TIMESTAMP)"
        ))
    with pytest.raises(SchemaMigrationError, match="999"):
        apply_migrations(engine, fake_migrations(), table=MIGRATION_TABLE)
    assert versions(engine) == {"999"}


def test_missing_earlier_version_is_not_skipped(handoff_tables):
    engine = handoff_tables
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE TABLE `{MIGRATION_TABLE}` (version VARCHAR(20) PRIMARY KEY, "
            "description VARCHAR(200) NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(text(
            f"INSERT INTO `{MIGRATION_TABLE}` (version, description, applied_at) "
            "VALUES ('002', 'later', CURRENT_TIMESTAMP)"
        ))
    with pytest.raises(SchemaMigrationError, match="001"):
        apply_migrations(engine, fake_migrations(), table=MIGRATION_TABLE)
    assert versions(engine) == {"002"}


def test_failed_migration_names_version_and_is_not_recorded(handoff_tables):
    engine = handoff_tables
    broken = fake_migrations()
    broken["002"] = Migration("002", "broken step", (
        "INSERT INTO `table_that_does_not_exist` (id) VALUES (1)",
    ))
    with pytest.raises(SchemaMigrationError, match="002"):
        apply_migrations(
            engine, broken, table=MIGRATION_TABLE,
            baseline_if_tables_exist=False, skip_existing_objects=False,
        )
    assert versions(engine) == {"001"}


def test_init_db_requires_schema_file_on_mysql(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "is_mysql", True)
    monkeypatch.setattr(db, "SCHEMA_FILE", tmp_path / "missing.sql")
    with pytest.raises(SchemaMigrationError, match="missing.sql"):
        db.init_db()


def test_init_db_uses_orm_metadata_on_sqlite(monkeypatch):
    called = []

    def fake_create_all(engine):
        called.append(engine)

    monkeypatch.setattr(db, "is_mysql", False)
    monkeypatch.setattr(db.Base.metadata, "create_all", fake_create_all)
    db.init_db()
    assert called == [db.engine]

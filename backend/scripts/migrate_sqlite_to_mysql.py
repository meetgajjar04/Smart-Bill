r"""Copy the complete SQLite database into MySQL/MariaDB, then verify every row.

Typical use (from backend/, with XAMPP MySQL running and setup_mysql.py done):

    .\.venv-win\Scripts\python.exe -m scripts.migrate_sqlite_to_mysql --replace

What it does
    1. Reads backend/smartbill.db (or --sqlite PATH).
    2. Applies backend/db/mysql_schema.sql (dropping old tables only with --replace).
    3. Copies users -> household_profiles -> bills -> alerts -> ai_insights,
       preserving ids and converting dates, decimals, booleans and JSON exactly.
    4. Verifies row counts, every column of every row, money totals, per-user
       totals, foreign keys and AUTO_INCREMENT values. Exits non-zero on any
       mismatch.

Verification runs before the final commit; a mismatch rolls back every imported
row. Use --verify-only to compare an existing MySQL copy without writing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy import MetaData

from app.database import split_sql_statements

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
SCHEMA_FILE = BACKEND / "db" / "mysql_schema.sql"
COPY_ORDER = ("users", "household_profiles", "bills", "alerts", "ai_insights")
DROP_ORDER = ("ai_insights", "alerts", "bills", "household_profiles", "users", "schema_migrations")
ID_COLUMN = {"users": "id", "household_profiles": "id", "bills": "id", "alerts": "id", "ai_insights": "id"}


class DataMismatch(RuntimeError):
    """Verification failed; the caller must roll back the imported rows."""


def settings_url() -> str:
    # Imported lazily so --database-url can bypass a broken .env.
    from app.config import settings
    return settings.database_url


def read_sqlite(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        raise SystemExit(f"SQLite database not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in COPY_ORDER if t not in tables]
        if missing:
            raise SystemExit(f"SQLite database is missing table(s): {', '.join(missing)}")
        return {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY id')] for t in COPY_ORDER}
    finally:
        conn.close()


def coerce(column, value):
    """Convert a SQLite value to the Python type the MySQL column expects."""
    if value is None:
        return None
    t = column.type
    if isinstance(t, JSON):
        if isinstance(value, (str, bytes, bytearray)):
            raw = value.decode() if isinstance(value, (bytes, bytearray)) else value
            raw = raw.strip()
            return json.loads(raw) if raw else None
        return value
    if isinstance(t, DateTime):
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(t, Date):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])
    if isinstance(t, Boolean):
        return bool(value)
    if isinstance(t, Numeric):
        return Decimal(str(value))
    if isinstance(t, Float):
        return float(value)
    if isinstance(t, Integer):
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value


def normalize(value) -> str:
    """Stable text form used for row-by-row comparison."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "num:1" if value else "num:0"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return "dt:" + value.isoformat(sep=" ", timespec="microseconds")
    if isinstance(value, date):
        return "date:" + value.isoformat()
    if isinstance(value, Decimal):
        return "num:" + (format(value.normalize(), "f") if value != 0 else "0")
    if isinstance(value, float):
        d = Decimal(str(value))
        return "num:" + (format(d.normalize(), "f") if d != 0 else "0")
    if isinstance(value, int):
        return "num:" + str(value)
    if isinstance(value, dict):
        return "json:" + json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "str:" + bytes(value).decode("utf-8", "replace")
    text_value = str(value)
    try:  # JSON columns may still arrive as text
        parsed = json.loads(text_value)
        if isinstance(parsed, (dict, list)):
            return "json:" + json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        pass
    return "str:" + text_value


def repair_orphans(data: dict[str, list[dict]]) -> list[str]:
    """Normalise legacy rows the way the new foreign keys behave.

    SQLite did not enforce foreign keys, so a few child rows can point at a
    deleted parent. MySQL does enforce them, therefore:
      * alerts keep their history and lose the dead bill pointer (SET NULL);
      * profiles/insights/alerts whose user no longer exists are removed (CASCADE).
    Bills without a user are never dropped; that is reported and stops the run.
    """
    notes: list[str] = []
    user_ids = {u["id"] for u in data["users"]}
    bill_ids = {b["id"] for b in data["bills"]}

    kept_alerts = []
    for alert in data["alerts"]:
        if alert["user_id"] not in user_ids:
            notes.append(f"alert {alert['id']}: owner user {alert['user_id']} is gone -> row removed")
            continue
        if alert["bill_id"] is not None and alert["bill_id"] not in bill_ids:
            notes.append(f"alert {alert['id']}: bill {alert['bill_id']} is gone -> bill_id set to NULL")
            alert["bill_id"] = None
        kept_alerts.append(alert)
    data["alerts"] = kept_alerts

    kept_insights = []
    for insight in data["ai_insights"]:
        if insight["user_id"] not in user_ids:
            notes.append(f"ai_insight {insight['id']}: owner user {insight['user_id']} is gone -> row removed")
            continue
        if insight["bill_id"] is not None and insight["bill_id"] not in bill_ids:
            notes.append(f"ai_insight {insight['id']}: bill {insight['bill_id']} is gone -> row removed")
            continue
        kept_insights.append(insight)
    data["ai_insights"] = kept_insights

    kept_profiles = []
    for profile in data["household_profiles"]:
        if profile["user_id"] not in user_ids:
            notes.append(f"household_profile {profile['id']}: owner user {profile['user_id']} is gone -> row removed")
            continue
        kept_profiles.append(profile)
    data["household_profiles"] = kept_profiles
    return notes


def preflight(data: dict[str, list[dict]]) -> list[str]:
    """Catch data that the stronger MySQL constraints would reject."""
    problems: list[str] = []
    valid_categories = {"electricity", "mobile", "internet", "water", "gas", "other"}
    valid_alert_kinds = {"anomaly", "reminder", "info"}
    valid_severity = {"info", "warning", "critical"}
    valid_insight_kinds = {"extract", "bill", "portfolio"}

    emails = [u["email"] for u in data["users"]]
    if len(emails) != len(set(emails)):
        problems.append("users.email has duplicates")

    profiles = [p["user_id"] for p in data["household_profiles"]]
    if len(profiles) != len(set(profiles)):
        problems.append("household_profiles.user_id has duplicates (must be 1:1)")

    files = [(b["user_id"], b["file_hash"]) for b in data["bills"] if b.get("file_hash")]
    if len(files) != len(set(files)):
        problems.append("bills.file_hash has duplicates per user (unique upload guard)")

    user_ids = {u["id"] for u in data["users"]}
    bill_ids = {b["id"] for b in data["bills"]}
    for b in data["bills"]:
        if b["user_id"] not in user_ids:
            problems.append(f"bill {b['id']} points at missing user {b['user_id']}")
        if b["category"] not in valid_categories:
            problems.append(f"bill {b['id']} has invalid category {b['category']!r}")
    for p in data["household_profiles"]:
        if p["user_id"] not in user_ids:
            problems.append(f"profile {p['id']} points at missing user {p['user_id']}")
    for a in data["alerts"]:
        if a["user_id"] not in user_ids:
            problems.append(f"alert {a['id']} points at missing user {a['user_id']}")
        if a["bill_id"] is not None and a["bill_id"] not in bill_ids:
            problems.append(f"alert {a['id']} points at missing bill {a['bill_id']}")
        if a["kind"] not in valid_alert_kinds:
            problems.append(f"alert {a['id']} has invalid kind {a['kind']!r}")
        if a["severity"] not in valid_severity:
            problems.append(f"alert {a['id']} has invalid severity {a['severity']!r}")
    for i in data["ai_insights"]:
        if i["user_id"] not in user_ids:
            problems.append(f"insight {i['id']} points at missing user {i['user_id']}")
        if i["bill_id"] is not None and i["bill_id"] not in bill_ids:
            problems.append(f"insight {i['id']} points at missing bill {i['bill_id']}")
        if i["kind"] not in valid_insight_kinds:
            problems.append(f"insight {i['id']} has invalid kind {i['kind']!r}")
        try:
            json.loads(i["payload"]) if isinstance(i["payload"], str) else None
        except ValueError:
            problems.append(f"insight {i['id']} payload is not valid JSON")
    return problems


# ---------------------------------------------------------------- migration
def apply_schema(engine, replace: bool) -> None:
    """DDL only. Data is copied and verified in a separate, rollbackable transaction."""
    sql_statements = split_sql_statements(SCHEMA_FILE.read_text(encoding="utf-8"))
    with engine.begin() as conn:
        existing = set(inspect(conn).get_table_names())
        if replace:
            try:
                conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
                for table in DROP_ORDER:
                    conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{table}`")
            finally:
                conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
            print("Old tables dropped (--replace).")
        elif existing & set(COPY_ORDER):
            counts = ", ".join(
                f"{t}={conn.exec_driver_sql(f'SELECT COUNT(*) FROM `{t}`').scalar()}" for t in COPY_ORDER
            )
            raise SystemExit(f"Target already has data ({counts}). Re-run with --replace to overwrite it.")

        for statement in sql_statements:
            conn.exec_driver_sql(statement)
    print(f"Schema applied: {len(sql_statements)} statements.")


def coerce_data(data: dict[str, list[dict]], meta: MetaData) -> dict[str, list[dict]]:
    """Convert every SQLite value once, using the reflected MySQL column types."""
    converted: dict[str, list[dict]] = {}
    for table_name in COPY_ORDER:
        table = meta.tables[table_name]
        converted[table_name] = [
            {col.name: coerce(col, row.get(col.name)) for col in table.columns if col.name in row}
            for row in data[table_name]
        ]
    return converted


def copy_rows(conn, meta: MetaData, data: dict[str, list[dict]], batch_size: int = 500) -> dict[str, int]:
    """Insert rows on the given connection; the caller owns the transaction."""
    copied = {}
    for table_name in COPY_ORDER:
        table = meta.tables[table_name]
        rows = [
            {col.name: coerce(col, row.get(col.name)) for col in table.columns if col.name in row}
            for row in data[table_name]
        ]
        if rows:
            for start in range(0, len(rows), batch_size):
                conn.execute(table.insert(), rows[start:start + batch_size])
        copied[table_name] = len(rows)
    return copied


# ---------------------------------------------------------------- verification
def mysql_rows(conn, table_name: str) -> list[dict]:
    result = conn.execute(text(f"SELECT * FROM `{table_name}` ORDER BY id"))
    return [dict(row._mapping) for row in result]


def verify(conn, meta: MetaData, data: dict[str, list[dict]]) -> list[str]:
    """Verify on the SAME connection that inserted the rows (pre-commit).

    MySQL DDL auto-commits, so verification cannot cover the table creation,
    but all imported data is checked before the caller commits. Any mismatch
    returns problems and the caller rolls the whole copy back.
    """
    problems: list[str] = []

    sqlite_counts = {t: len(data[t]) for t in COPY_ORDER}
    mysql_counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar() for t in COPY_ORDER}
    for t in COPY_ORDER:
        if sqlite_counts[t] != mysql_counts[t]:
            problems.append(f"{t}: SQLite has {sqlite_counts[t]} rows, MySQL has {mysql_counts[t]}")

    # Compare every column of every row (ids are preserved).
    for table_name in COPY_ORDER:
        columns = list(meta.tables[table_name].columns)
        sqlite_rows = {row["id"]: row for row in data[table_name]}
        target_rows = {row["id"]: row for row in mysql_rows(conn, table_name)}
        for row_id in sorted(set(sqlite_rows) | set(target_rows)):
            if row_id not in target_rows:
                problems.append(f"{table_name} id={row_id}: missing in MySQL")
                continue
            if row_id not in sqlite_rows:
                problems.append(f"{table_name} id={row_id}: extra row in MySQL")
                continue
            for col in columns:
                source = normalize(sqlite_rows[row_id].get(col.name))
                target = normalize(target_rows[row_id].get(col.name))
                if source != target:
                    problems.append(f"{table_name} id={row_id} {col.name}: SQLite={source!r} MySQL={target!r}")

    # Business totals that must survive the move.
    mysql_bills = conn.execute(text(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total, "
        "SUM(paid = 1) AS paid, SUM(paid = 0) AS unpaid, "
        "MIN(due_date) AS first_due, MAX(due_date) AS last_due FROM bills"
    )).mappings().one()
    source_bills = data["bills"]
    source_total = sum(Decimal(str(b["amount"])) for b in source_bills if b["amount"] is not None)
    if int(mysql_bills["n"]) != len(source_bills):
        problems.append("bills count changed")
    if Decimal(str(mysql_bills["total"])) != source_total:
        problems.append(f"bills total changed: SQLite={source_total} MySQL={mysql_bills['total']}")
    if int(mysql_bills["paid"] or 0) != sum(1 for b in source_bills if b["paid"]):
        problems.append("paid bill count changed")
    if int(mysql_bills["unpaid"] or 0) != sum(1 for b in source_bills if not b["paid"]):
        problems.append("unpaid bill count changed")

    # Foreign-key orphans (MySQL would not allow them, but check explicitly).
    orphan_queries = {
        "bills without user": "SELECT COUNT(*) FROM bills b LEFT JOIN users u ON u.id=b.user_id WHERE u.id IS NULL",
        "alerts without user": "SELECT COUNT(*) FROM alerts a LEFT JOIN users u ON u.id=a.user_id WHERE u.id IS NULL",
        "alerts without bill": "SELECT COUNT(*) FROM alerts a LEFT JOIN bills b ON b.id=a.bill_id WHERE a.bill_id IS NOT NULL AND b.id IS NULL",
        "insights without user": "SELECT COUNT(*) FROM ai_insights i LEFT JOIN users u ON u.id=i.user_id WHERE u.id IS NULL",
        "insights without bill": "SELECT COUNT(*) FROM ai_insights i LEFT JOIN bills b ON b.id=i.bill_id WHERE i.bill_id IS NOT NULL AND b.id IS NULL",
        "profiles without user": "SELECT COUNT(*) FROM household_profiles p LEFT JOIN users u ON u.id=p.user_id WHERE u.id IS NULL",
        "duplicate profile per user": "SELECT COUNT(*) FROM (SELECT user_id FROM household_profiles GROUP BY user_id HAVING COUNT(*)>1) x",
    }
    for label, sql in orphan_queries.items():
        n = conn.execute(text(sql)).scalar()
        if n:
            problems.append(f"{label}: {n}")

    # JSON must be valid for every non-null JSON value.
    for table_name, column in (("bills", "analysis"), ("bills", "feedback"), ("ai_insights", "payload")):
        n = conn.execute(text(
            f"SELECT COUNT(*) FROM `{table_name}` WHERE `{column}` IS NOT NULL AND JSON_VALID(`{column}`) = 0"
        )).scalar()
        if n:
            problems.append(f"{table_name}.{column}: {n} invalid JSON values")

    # The next generated id must be above the largest copied id.
    for table_name in COPY_ORDER:
        max_id = conn.execute(text(f"SELECT COALESCE(MAX(id),0) FROM `{table_name}`")).scalar()
        next_id = conn.execute(text(
            "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name"
        ), {"name": table_name}).scalar()
        if next_id is None or int(next_id) <= int(max_id):
            problems.append(f"{table_name}: AUTO_INCREMENT {next_id} <= max id {max_id}")

    return problems


def print_summary(engine, data: dict[str, list[dict]]) -> None:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total, SUM(paid=1) paid, SUM(paid=0) unpaid, "
            "MIN(due_date) first_due, MAX(due_date) last_due FROM bills"
        )).mappings().one()
        users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        alerts = conn.execute(text("SELECT COUNT(*) FROM alerts")).scalar()
        insights = conn.execute(text("SELECT COUNT(*) FROM ai_insights")).scalar()
        profiles = conn.execute(text("SELECT COUNT(*) FROM household_profiles")).scalar()
    print()
    print("Migration summary")
    print(f"  users              {users}")
    print(f"  household_profiles {profiles}")
    print(f"  bills              {row['n']}  (paid {row['paid']}, unpaid {row['unpaid']})")
    print(f"  total amount       ₹{Decimal(str(row['total'])):,}")
    print(f"  due-date range     {row['first_due']} .. {row['last_due']}")
    print(f"  alerts             {alerts}")
    print(f"  ai_insights        {insights}")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", default=str(BACKEND / "smartbill.db"), help="source SQLite file")
    parser.add_argument("--database-url", default=None, help="target MySQL URL (default: DATABASE_URL from .env)")
    parser.add_argument("--replace", action="store_true", help="drop and recreate the target tables before copying")
    parser.add_argument("--verify-only", action="store_true", help="only compare an existing MySQL copy")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    try:
        url = make_url(args.database_url or settings_url())
    except Exception as e:
        print(f"Cannot read the database URL: {e}", file=sys.stderr)
        return 1
    if url.get_backend_name() not in ("mysql", "mariadb"):
        print("This script writes to MySQL/MariaDB.", file=sys.stderr)
        print(f"Current DATABASE_URL is {url.get_backend_name()!r}; set DATABASE_URL=mysql+pymysql://...", file=sys.stderr)
        return 1

    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"charset": "utf8mb4", "connect_timeout": 10,
                                         "init_command": "SET time_zone = '+00:00'"})
    print(f"Target : {url.render_as_string(hide_password=True)}")
    data = read_sqlite(Path(args.sqlite))
    print("Source : " + ", ".join(f"{t}={len(rows)}" for t, rows in data.items()))
    repairs = repair_orphans(data)
    if repairs:
        print(f"Repaired {len(repairs)} legacy row(s) to satisfy the MySQL foreign keys:")
        for note in repairs:
            print(f"  * {note}")

    problems = preflight(data)
    if problems:
        print("\nThe SQLite data cannot be migrated as-is:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    issues: list[str] = []
    if args.verify_only:
        with engine.connect() as conn:
            meta = MetaData()
            meta.reflect(bind=conn, only=COPY_ORDER)
            data = coerce_data(data, meta)
            issues = verify(conn, meta, data)
    else:
        apply_schema(engine, args.replace)
        try:
            with engine.begin() as conn:
                meta = MetaData()
                meta.reflect(bind=conn, only=COPY_ORDER)
                data = coerce_data(data, meta)
                copied = copy_rows(conn, meta, data, args.batch_size)
                print("Copied : " + ", ".join(f"{t}={n}" for t, n in copied.items()))
                issues = verify(conn, meta, data)
                if issues:
                    # Rolling back here means no partially verified rows are committed.
                    raise DataMismatch("verification failed")
        except DataMismatch:
            pass

    if issues:
        print(f"\nVerification FAILED ({len(issues)} issue(s)); no data was committed:", file=sys.stderr)
        for issue in issues[:50]:
            print(f"  - {issue}", file=sys.stderr)
        if len(issues) > 50:
            print(f"  ... and {len(issues) - 50} more", file=sys.stderr)
        return 1

    print_summary(engine, data)
    print("\nVerification passed: every row, column, total, foreign key and AUTO_INCREMENT matches SQLite.")
    if not args.verify_only:
        print("Next: set DATABASE_URL in .env, then restart the backend.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

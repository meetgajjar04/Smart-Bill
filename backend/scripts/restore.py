r"""Restore a mysqldump file into a MySQL/MariaDB database (with safety checks).

    cd backend
    python -m scripts.restore --file ..\backups\smartbill_20260101_090000.sql `
        --database smartbill_restore_test --yes

Safety:
  * ``--database`` and ``--yes`` are mandatory; the target must end in ``_test``
    unless ``--allow-live`` is given (and then the name is printed before import).
  * Admin credentials come from MYSQL_ROOT_PASSWORD / MYSQL_ADMIN_PASSWORD or an
    interactive prompt, never from argv. The password is passed to the mysql
    client through MYSQL_PWD, not on its command line.
  * The recommended restore test imports into a throwaway ``*_test`` database,
    prints row counts, and drops that database afterwards (see SETUP-WINDOWS.md).
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

import pymysql
from app.config import settings

from .dbtools import find_client, masked_url, mysql_credentials, parse_mysql_url, run_client, validate_identifier

BACKEND = Path(__file__).resolve().parents[1]


def admin_password() -> str:
    secret = os.getenv("MYSQL_ROOT_PASSWORD") or os.getenv("MYSQL_ADMIN_PASSWORD")
    if secret is not None:
        return secret
    if not sys.stdin.isatty():
        raise SystemExit("Set MYSQL_ROOT_PASSWORD (or MYSQL_ADMIN_PASSWORD) for non-interactive restores.")
    return getpass.getpass("MySQL admin password: ")


def restore_database(
    url,
    dump: Path,
    database: str,
    *,
    admin_user: str = "root",
    password: str | None = None,
    yes: bool = False,
    allow_live: bool = False,
    client_path: str | None = None,
    uploads_from: Path | None = None,
    uploads_dir: Path | None = None,
) -> dict[str, int]:
    url = parse_mysql_url(url)
    validate_identifier(database, "target database")
    if not database.endswith("_test") and not allow_live:
        raise SystemExit(
            f"Refusing to import into {database!r}: use a throwaway *_test database for the "
            "restore test, or pass --allow-live for a deliberate production restore."
        )
    if not yes:
        raise SystemExit("Refusing to import without --yes (the dump overwrites tables in the target).")
    if not dump.is_file():
        raise SystemExit(f"Dump file not found: {dump}")

    secret = admin_password() if password is None else password
    host, port = url.host or "127.0.0.1", url.port or 3306
    print(f"Target : {masked_url(url)} (database {database}, admin user {admin_user})")
    print(f"Dump   : {dump}")
    if uploads_from:
        print(f"Uploads: {uploads_from}")

    # CREATE DATABASE needs admin rights; the app user only gets SELECT/INSERT/UPDATE/DELETE.
    admin = pymysql.connect(
        host=host, port=port, user=admin_user, password=secret,
        charset="utf8mb4", autocommit=True, connect_timeout=10,
    )
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        admin.close()

    client = client_path or find_client("mysql")
    with mysql_credentials(url, user=admin_user, password=secret) as (config, env):
        with dump.open("r", encoding="utf-8") as stream:
            run_client(
                client,
                [f"--defaults-extra-file={config}", database],
                env, stdin=stream, secrets=(secret,),
            )

    if uploads_from:
        if not uploads_from.is_dir():
            raise SystemExit(f"Uploads folder not found: {uploads_from}")
        destination = uploads_dir or (BACKEND / "uploads")
        for entry in uploads_from.iterdir():
            if entry.is_dir():
                shutil.copytree(entry, destination / entry.name, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, destination / entry.name)
        print(f"Restored uploads into {destination}")

    admin = pymysql.connect(
        host=host, port=port, user=admin_user, password=secret,
        database=database, charset="utf8mb4", autocommit=True, connect_timeout=10,
    )
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            counts = {str(name): int(rows or 0) for name, rows in cur.fetchall()}
    finally:
        admin.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="mysqldump .sql file to import")
    parser.add_argument("--database", required=True, help="target database (must end in _test unless --allow-live)")
    parser.add_argument("--database-url", default=settings.database_url, help="server URL (default: DATABASE_URL from .env)")
    parser.add_argument("--admin-user", default="root", help="admin MySQL user (default: root)")
    parser.add_argument("--yes", action="store_true", help="confirm overwrite of the target database")
    parser.add_argument("--allow-live", action="store_true", help="allow a non-*_test target (dangerous)")
    parser.add_argument("--uploads-from", type=Path, default=None, help="optional backup uploads_<stamp> folder")
    parser.add_argument("--client-path", default=None, help="explicit path to the mysql client")
    args = parser.parse_args()

    try:
        counts = restore_database(
            parse_mysql_url(args.database_url), args.file, args.database,
            admin_user=args.admin_user, yes=args.yes, allow_live=args.allow_live,
            client_path=args.client_path, uploads_from=args.uploads_from,
        )
    except (SystemExit, Exception) as exc:
        print(f"Restore FAILED: {exc}", file=sys.stderr)
        return 1

    print("Restored row counts:")
    for table, rows in counts.items():
        print(f"  {table:<20} {rows}")
    print("Verify with: SELECT COUNT(*) FROM bills; then drop the *_test database when done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

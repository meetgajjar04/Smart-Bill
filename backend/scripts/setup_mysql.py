r"""Create the Smart Bill AI database and app user in MySQL/MariaDB, then apply schema.

Run this once (XAMPP Control Panel -> MySQL -> Start first):

    cd backend
    .\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "choose-a-password"

It uses the XAMPP root account only for provisioning. The application then
connects as the limited `smartbill` user, never as root.

Options:
    --host 127.0.0.1 --port 3306
    --database smartbill --app-user smartbill
    --root-user root --root-password ""      (use MYSQL_ROOT_PASSWORD; XAMPP default is blank)
    --app-password SECRET                     (or MYSQL_APP_PASSWORD in the environment)
    --reset-password                          explicitly reset an existing app user's password
    --replace --yes                           drop existing tables and rebuild (DESTRUCTIVE;
                                              a mysqldump backup is taken first)
    --retain N                                number of automatic backups to keep (default 7)
    --start-xampp                             start C:\xampp MySQL if it is not running

The script is idempotent: it creates what is missing, applies pending schema
migrations in order, and never changes an existing password unless asked to.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pymysql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from app.database import apply_migrations
from .backup import backup_database
from .dbtools import masked_url, validate_identifier

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
SCHEMA_FILE = BACKEND / "db" / "mysql_schema.sql"
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
DROP_ORDER = ("ai_insights", "alerts", "bills", "household_profiles", "users", "schema_migrations")


def connect(host: str, port: int, user: str, password: str, database: str | None = None):
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database,
        charset="utf8mb4", autocommit=True, connect_timeout=10,
        init_command="SET time_zone = '+00:00'",
    )


def port_open(host: str, port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def start_xampp(host: str, port: int) -> None:
    """Start the MySQL server shipped with XAMPP and wait until it accepts TCP."""
    if port_open(host, port):
        return
    print("MySQL is not running; starting XAMPP MySQL...")
    if sys.platform == "win32":
        mysqld = shutil.which("mysqld") or r"C:\xampp\mysql\bin\mysqld.exe"
        if not Path(mysqld).exists():
            raise SystemExit(f"mysqld.exe not found at {mysqld}. Start MySQL from the XAMPP Control Panel.")
        ini = Path(mysqld).with_name("my.ini")
        args = [mysqld, f"--defaults-file={ini}", "--standalone"]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif Path("/opt/lampp/lampp").exists():
        subprocess.run(["/opt/lampp/lampp", "startmysql"], check=False)
    elif Path("/Applications/XAMPP/xamppfiles/bin/mysqld").exists():
        mysqld = "/Applications/XAMPP/xamppfiles/bin/mysqld"
        subprocess.Popen([mysqld, "--standalone"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise SystemExit("Start MySQL first (XAMPP Control Panel), then run this script again.")

    for _ in range(60):
        if port_open(host, port):
            print("MySQL is up.")
            return
        time.sleep(0.5)
    raise SystemExit("MySQL did not start within 30 seconds. Check the XAMPP log.")


def current_env_password() -> str:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                try:
                    url = make_url(value)
                except Exception:
                    continue
                if url.get_backend_name() in ("mysql", "mariadb") and url.password:
                    return url.password
    return ""


def confirm_replace(database: str) -> None:
    print(f"DESTRUCTIVE: --replace will DROP all tables in {database!r} before rebuilding them.")
    if sys.stdin.isatty():
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            raise SystemExit("Aborted: --replace was not confirmed.")
    else:
        raise SystemExit("Non-interactive shell: re-run with --replace --yes to confirm the drop.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--database", default="smartbill")
    parser.add_argument("--app-user", default="smartbill")
    parser.add_argument("--app-password", default=os.getenv("MYSQL_APP_PASSWORD") or current_env_password())
    parser.add_argument("--root-user", default=os.getenv("MYSQL_ROOT_USER", "root"))
    parser.add_argument("--root-password", default=os.getenv("MYSQL_ROOT_PASSWORD", ""))
    parser.add_argument("--reset-password", action="store_true",
                        help="explicitly change the existing app user's password")
    parser.add_argument("--replace", action="store_true", help="drop existing tables before creating them (DESTRUCTIVE)")
    parser.add_argument("--yes", action="store_true", help="confirm --replace in a non-interactive shell")
    parser.add_argument("--retain", type=int, default=7, help="automatic backups to keep before --replace")
    parser.add_argument("--backup-dir", default=str(ROOT / "backups"))
    parser.add_argument("--client-path", default=None, help="explicit path to mysqldump")
    parser.add_argument("--show-full-url", action="store_true",
                        help="print the DATABASE_URL including its password (off by default)")
    parser.add_argument("--start-xampp", action="store_true", help="start XAMPP MySQL if it is not running")
    args = parser.parse_args()

    try:
        validate_identifier(args.database, "database")
        validate_identifier(args.app_user, "app user")
    except ValueError as exc:
        parser.error(str(exc))
    if not args.app_password:
        parser.error("--app-password is required (or set MYSQL_APP_PASSWORD)")
    if not SCHEMA_FILE.exists():
        parser.error(f"schema file missing: {SCHEMA_FILE}")

    db = args.database
    print(f"Target database : {db} at {args.host}:{args.port}")
    if args.replace and not args.yes:
        try:
            confirm_replace(db)
        except SystemExit as exc:
            parser.error(str(exc))

    if args.start_xampp:
        start_xampp(args.host, args.port)

    # 1. Provision database + application user with the root account.
    try:
        root = connect(args.host, args.port, args.root_user, args.root_password)
    except pymysql.err.OperationalError as e:
        print(f"Cannot connect to MySQL at {args.host}:{args.port} as {args.root_user!r}: {e}", file=sys.stderr)
        print("Start MySQL in the XAMPP Control Panel, then run this script again.", file=sys.stderr)
        return 1

    root_url = URL.create(
        "mysql+pymysql", username=args.root_user, password=args.root_password,
        host=args.host, port=args.port, database=db,
    )
    try:
        with root.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            for host in ("localhost", "127.0.0.1"):
                cur.execute(
                    f"CREATE USER IF NOT EXISTS '{args.app_user}'@'{host}' IDENTIFIED BY %s",
                    (args.app_password,),
                )
                if args.reset_password:
                    print(f"Resetting password for {args.app_user}@{host} (explicit --reset-password).")
                    cur.execute(
                        f"ALTER USER '{args.app_user}'@'{host}' IDENTIFIED BY %s",
                        (args.app_password,),
                    )
                # Production app user is data-only. Test databases get DDL rights so
                # the test suite (which drops/recreates tables) can use the same user.
                privileges = "ALL PRIVILEGES" if db.endswith("_test") else "SELECT, INSERT, UPDATE, DELETE"
                cur.execute(f"GRANT {privileges} ON `{db}`.* TO '{args.app_user}'@'{host}'")
            cur.execute("FLUSH PRIVILEGES")

        # 2. Back up the existing database BEFORE any destructive --replace.
        if args.replace:
            dump, _ = backup_database(
                root_url, Path(args.backup_dir), retain=args.retain, client_path=args.client_path,
            )
            print(f"Automatic backup before --replace: {dump}")

        root.select_db(db)
        if args.replace:
            with root.cursor() as cur:
                try:
                    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
                    for table in DROP_ORDER:
                        cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                finally:
                    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            print("Existing tables dropped (--replace).")

        # 3. Apply the ordered, versioned schema as root (the app user is CRUD-only).
        schema_engine = create_engine(root_url, pool_pre_ping=True)
        try:
            applied = apply_migrations(schema_engine)
        finally:
            schema_engine.dispose()

        with root.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT version, description FROM schema_migrations ORDER BY version")
            migrations = cur.fetchall()
    finally:
        root.close()

    # 4. Prove the application login works (it only needs data rights).
    app = connect(args.host, args.port, args.app_user, args.app_password, db)
    try:
        with app.cursor() as cur:
            cur.execute("SELECT DATABASE(), VERSION()")
            database_name, server_version = cur.fetchone()
    finally:
        app.close()

    print()
    print(f"Database   : {database_name}")
    print(f"Server     : {server_version}")
    print(f"Tables     : {', '.join(tables)}")
    print(f"Migrations : {', '.join(f'{v} ({d})' for v, d in migrations)}")
    if applied:
        print(f"Applied now: {', '.join(applied)}")
    print(f"App user   : {args.app_user}@localhost / 127.0.0.1 "
          f"({'ALL on *_test' if db.endswith('_test') else 'SELECT, INSERT, UPDATE, DELETE'})")
    print()
    print("Put this in the project's .env (password hidden by default):")
    app_url = URL.create("mysql+pymysql", username=args.app_user, password=args.app_password,
                         host=args.host, port=args.port, database=db)
    print(f"DATABASE_URL={masked_url(app_url)}")
    if args.show_full_url:
        url_password = quote(args.app_password, safe="")
        print(f"DATABASE_URL (full, sensitive): mysql+pymysql://{args.app_user}:{url_password}@{args.host}:{args.port}/{db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

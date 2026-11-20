r"""Back up the MySQL/MariaDB database (and optionally uploaded files).

    cd backend
    python -m scripts.backup                       # uses DATABASE_URL from .env
    python -m scripts.backup --retain 14 --uploads

The dump is written to <project>/backups/<database>_YYYYmmdd_HHMMSS.sql. Older
dumps for the same database are pruned to --retain (default 7). Credentials are
never placed on the command line or in logs: a temporary defaults-extra-file
carries host/port/user and the password is passed via MYSQL_PWD.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.config import settings

from .dbtools import (
    find_client, masked_url, mysql_credentials, parse_mysql_url,
    prune, run_client, timestamp,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
DEFAULT_BACKUP_DIR = ROOT / "backups"


def backup_database(
    url,
    backup_dir: Path,
    *,
    retain: int = 7,
    client_path: str | None = None,
    do_uploads: bool = False,
    uploads_dir: Path | None = None,
) -> tuple[Path, list[Path]]:
    url = parse_mysql_url(url)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    target = backup_dir / f"{url.database}_{stamp}.sql"

    with mysql_credentials(url) as (config, env):
        client = client_path or find_client("mysqldump")
        args = [
            f"--defaults-extra-file={config}",
            "--single-transaction", "--skip-lock-tables", "--hex-blob",
            "--routines", "--triggers",
            url.database,
            f"--result-file={target}",
        ]
        run_client(client, args, env, secrets=(url.password,))

    if do_uploads:
        source = uploads_dir or (BACKEND / "uploads")
        if source.exists():
            destination = backup_dir / f"uploads_{stamp}"
            shutil.copytree(source, destination, dirs_exist_ok=True)
            print(f"Uploads : {destination}")

    removed = prune(backup_dir, url.database, retain)
    return target, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=settings.database_url, help="MySQL URL (default: DATABASE_URL from .env)")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--retain", type=int, default=7, help="number of recent dumps to keep (default: 7)")
    parser.add_argument("--uploads", action="store_true", help="also copy the uploads folder")
    parser.add_argument("--client-path", default=None, help="explicit path to mysqldump")
    args = parser.parse_args()

    try:
        url = parse_mysql_url(args.database_url)
        target, removed = backup_database(
            url, Path(args.backup_dir), retain=args.retain,
            client_path=args.client_path, do_uploads=args.uploads,
        )
    except Exception as exc:
        print(f"Backup FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Target  : {masked_url(url)}")
    print(f"Backup  : {target}")
    if removed:
        print(f"Pruned  : {len(removed)} older dump(s)")
    print("Restore test: python -m scripts.restore --file " + str(target) +
          " --database <name>_test --yes   (see SETUP-WINDOWS.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

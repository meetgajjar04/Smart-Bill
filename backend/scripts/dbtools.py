"""Shared MySQL/MariaDB client plumbing for setup, backup and restore scripts.

Passwords never appear in argv or in logs: a temporary ``--defaults-extra-file``
carries host/port/user and the password is supplied through the ``MYSQL_PWD``
environment variable (which the mysql/mariadb clients read). A caller may also
supply its own defaults file with credentials via ``--defaults-extra-file``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import URL, make_url

IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def parse_mysql_url(raw: str | URL) -> URL:
    url = raw if isinstance(raw, URL) else make_url(raw)
    if url.get_backend_name() not in ("mysql", "mariadb"):
        raise ValueError(f"Expected a MySQL/MariaDB URL, got {url.get_backend_name()!r}")
    if not url.database or not IDENTIFIER.fullmatch(url.database):
        raise ValueError(f"Invalid database name in URL: {url.database!r}")
    return url


def masked_url(url: URL) -> str:
    """URL safe to print (password replaced by ***) — the default for all logs."""
    return url.render_as_string(hide_password=True)


def validate_identifier(name: str, label: str = "name") -> str:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid {label}: {name!r} (letters, digits and _ only)")
    return name


def find_client(name: str) -> str:
    """Find mysqldump/mysql, including the XAMPP and MariaDB install locations."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        Path(r"C:\xampp\mysql\bin") / f"{name}.exe",
        Path("/opt/lampp/bin") / name,
        Path("/Applications/XAMPP/xamppfiles/bin") / name,
        Path("/usr/bin") / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"{name} not found. Add C:\\xampp\\mysql\\bin to PATH or pass --client-path.")


@contextmanager
def mysql_credentials(
    url: URL,
    *,
    user: str | None = None,
    password: str | None = None,
):
    """Yield ``(defaults_file, env)`` for a client invocation.

    ``password=None`` means "use the password from the URL"; pass an empty
    string to intentionally use no password.
    """
    username = user or url.username or "root"
    secret = url.password if password is None else password
    host, port = url.host or "127.0.0.1", url.port or 3306

    with tempfile.TemporaryDirectory(prefix="smartbill-mysql-") as tmp:
        config = Path(tmp) / "client.cnf"
        config.write_text(
            f"[client]\nhost={host}\nport={port}\nuser={username}\n", encoding="utf-8"
        )
        try:
            config.chmod(0o600)
        except OSError:
            pass  # Windows file permissions are managed through the ACL, not chmod.
        env = os.environ.copy()
        if secret:
            env["MYSQL_PWD"] = secret
        else:
            env.pop("MYSQL_PWD", None)
        yield str(config), env


def redact(text: str, *passwords: str | None) -> str:
    """Defensive scrubbing in case a tool echoes a credential in an error."""
    for secret in passwords:
        if secret:
            text = text.replace(secret, "***")
    return text


def run_client(
    client: str,
    args: list[str],
    env: dict[str, str],
    *,
    stdin=None,
    secrets: tuple[str | None, ...] = (),
) -> str:
    proc = subprocess.run(
        [client, *args], env=env, stdin=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode:
        detail = redact((proc.stderr or proc.stdout or "no output").strip(), *secrets)
        raise RuntimeError(f"{Path(client).name} exited with code {proc.returncode}: {detail[:800]}")
    return redact(proc.stdout, *secrets)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def prune(backup_dir: Path, prefix: str, retain: int, suffix: str = ".sql") -> list[Path]:
    """Keep the newest ``retain`` files matching ``prefix_*<suffix>``."""
    if retain < 1:
        raise ValueError("--retain must be at least 1")
    files = sorted(backup_dir.glob(f"{prefix}_*{suffix}"), reverse=True)
    removed = files[retain:]
    for old in removed:
        old.unlink()
    return removed

import os
import tempfile

_tmp = tempfile.mkdtemp()

# Tests use a throwaway SQLite file by default. Point SMARTBILL_TEST_DATABASE_URL
# at a MySQL database (name must end with _test) to run the identical suite on
# the production engine:   SMARTBILL_TEST_DATABASE_URL=mysql+pymysql://...
TEST_DATABASE_URL = os.environ.get("SMARTBILL_TEST_DATABASE_URL", "").strip()

if TEST_DATABASE_URL:
    from sqlalchemy.engine import make_url

    _url = make_url(TEST_DATABASE_URL)
    _backend = _url.get_backend_name()
    _name = _url.database or ""
    if _backend in ("mysql", "mariadb"):
        _safe = _name.endswith("_test")
    elif _backend == "sqlite":
        _safe = _name in (":memory:", "") or _name.replace("\\", "/").endswith("_test.db")
    else:
        _safe = False
    if not _safe:
        raise RuntimeError(
            "SMARTBILL_TEST_DATABASE_URL must point at a throwaway database: a MySQL name "
            f"ending in _test or a SQLite *_test.db file (got {_name!r}); the suite drops all tables."
        )

# Pin test secrets so a developer's real .env cannot change test behaviour.
os.environ.update(
    APP_ENV="development",
    DATABASE_URL=TEST_DATABASE_URL or f"sqlite:///{_tmp}/smartbill_test.db",
    UPLOAD_DIR=f"{_tmp}/up",
    N8N_WEBHOOK_URL="",
    SECRET_KEY="test-secret-key-that-is-long-enough-123456",
    RATE_LIMIT_SECRET="unit-test-rate-limit-key-0123456789abcdef",
    INTERNAL_API_KEY="dev-internal-key",
    N8N_WEBHOOK_SECRET="dev-webhook-secret",
    OLLAMA_ENABLED="false",
    OLLAMA_MODEL="",
    EMAIL_ENABLED="false",
    SMTP_HOST="",
    SMTP_FROM="",
)

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import CSRF_COOKIE

H = {"X-Requested-With": "SmartBillAI"}


def auth_headers(client) -> dict[str, str]:
    """Baseline headers plus this client's current signed double-submit token."""
    return {**H, "X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "")}


@pytest.fixture(scope="session", autouse=True)
def _clean_database():
    """Start every run from an empty schema.

    SQLite already lives in a fresh temp directory; a MySQL target is reset here
    so repeated runs do not accumulate rows. The name guard keeps an accidental
    SMARTBILL_TEST_DATABASE_URL from wiping a real database.
    """
    from app.database import Base, MIGRATION_TABLE, engine, init_db

    if TEST_DATABASE_URL:
        # Drop base tables and the version ledger together. Keeping an old
        # "001 applied" row after drop_all would skip CREATE TABLE and leave
        # migration 002 trying to ALTER a missing `alerts` table.
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{MIGRATION_TABLE}`")
    # Create the schema before any test runs: the autouse limiter reset needs
    # rate_limit_hits even in tests that never start the TestClient lifespan.
    init_db()
    yield


@pytest.fixture(scope="session")
def client(_clean_database):
    with TestClient(app) as c:
        yield c


def make_user(client_cls, email):
    c = client_cls(app, headers=H)
    r = c.post("/api/auth/register", json={"name": "T", "email": email, "password": "Sup3rSecret99"})
    assert r.status_code == 201, r.text
    csrf = c.cookies.get(CSRF_COOKIE)
    assert csrf, "registration must set the readable CSRF cookie"
    c.headers["X-CSRF-Token"] = csrf
    return c


@pytest.fixture(autouse=True)
def _realistic_test_client_ip(monkeypatch):
    """Starlette's TestClient uses the non-IP peer 'testclient'.

    Production sockets always have an IP; pin a normal address so API tests use
    the ordinary login/registration buckets instead of the strict unknown one.
    Tests that exercise client_ip itself call the security helper directly, and
    the unknown-bucket test overrides this fixture value.
    """
    import app.routers.auth as auth

    monkeypatch.setattr(auth, "client_ip", lambda request: "198.51.100.10")


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Each test starts with empty shared buckets (no in-memory limiter exists)."""
    from app.database import SessionLocal
    from app.models import RateLimitHit

    with SessionLocal() as db:
        db.query(RateLimitHit).delete(synchronize_session=False)
        db.commit()

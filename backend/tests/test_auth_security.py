"""Regression tests for the auth hardening reviews."""
import hashlib
import inspect
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import PropertyMock

import jwt
import pytest
from argon2.exceptions import InvalidHashError
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.config as config
import app.security as security
from app.config import Settings, settings
from app.database import SessionLocal
from app.models import RateLimitHit, User
from app.security import (
    CSRF_COOKIE, _retry_after, client_ip, create_token, decode_token, fingerprint,
    hash_password, password_needs_rehash, rate_limit, safe_equal, verify_password,
)

from .conftest import H, make_user

PASSWORD = "Sup3rSecret99"
NEW_PASSWORD = "An0therSecret99"


class _Headers(dict):
    """Mimic Starlette's multi-value headers for client_ip tests."""

    def getlist(self, name):
        value = self.get(name, [])
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value] if value else []


def _request(host="198.51.100.10", forwarded=None, real_ip=None):
    headers = _Headers()
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    if real_ip is not None:
        headers["x-real-ip"] = real_ip
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


def _login(app, email, password=PASSWORD):
    c = TestClient(app, headers=H)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = c.cookies.get(CSRF_COOKIE, "")
    return c


def _token_payload():
    now = datetime.now(timezone.utc)
    return {
        "sub": "1", "iat": now, "exp": now + timedelta(minutes=30),
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
        "jti": "jti-test", "ver": 0, "csrf": "csrf-test",
    }


def test_jwt_requires_every_claim_pinned_alg_and_known_key_id(client):
    assert decode_token(create_token(1, 0, "csrf-test")).user_id == 1

    for missing in ("exp", "iat", "sub", "iss", "aud", "jti", "ver", "csrf"):
        payload = _token_payload()
        del payload[missing]
        token = jwt.encode(payload, settings.secret_key, algorithm="HS256",
                           headers={"kid": settings.jwt_key_id})
        assert decode_token(token) is None, f"missing {missing} was accepted"

    payload = _token_payload()
    assert decode_token(jwt.encode(payload, "", algorithm="none")) is None
    assert decode_token(jwt.encode(payload, settings.secret_key, algorithm="HS256",
                                   headers={"kid": "retired-key"})) is None


def test_placeholder_or_short_secret_is_refused_in_every_environment(client):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(app_env="development", secret_key="change-me-short").assert_safe()


def test_retired_signing_key_is_accepted_during_rotation(monkeypatch, client):
    old_key = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "jwt_previous_secret_keys", old_key)
    token = jwt.encode(_token_payload(), old_key, algorithm="HS256",
                       headers={"kid": "previous-1"})
    assert decode_token(token).user_id == 1


def test_safe_equal_accepts_attacker_controlled_unicode(client):
    assert safe_equal("åß-é", "åß-é")
    assert not safe_equal("åß-é", "åß-e")


def test_csrf_cookie_header_and_jwt_claim_must_all_match(client):
    c = make_user(TestClient, "csrf@example.com")

    missing = TestClient(c.app, headers=H)
    missing.cookies.update(c.cookies)
    assert missing.patch("/api/auth/me", json={"notify_email": False}).status_code == 403

    wrong = TestClient(c.app, headers={**H, "X-CSRF-Token": "wrong"})
    wrong.cookies.update(c.cookies)
    assert wrong.patch("/api/auth/me", json={"notify_email": False}).status_code == 403


def test_untrusted_origin_is_rejected_even_for_login(client):
    c = make_user(TestClient, "origin@example.com")
    response = c.patch("/api/auth/me", json={"notify_email": False},
                       headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Origin."
    assert c.patch("/api/auth/me", json={"notify_email": False},
                   headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_logout_all_revokes_tokens_on_other_devices(client):
    email = "logout-all@example.com"
    first = make_user(TestClient, email)
    second = _login(first.app, email)
    assert second.get("/api/auth/me").status_code == 200

    assert first.post("/api/auth/logout-all").status_code == 204
    assert first.get("/api/auth/me").status_code == 401
    assert second.get("/api/auth/me").status_code == 401
    assert _login(first.app, email).get("/api/auth/me").status_code == 200


def test_password_change_revokes_other_sessions(client):
    email = "change-password@example.com"
    first = make_user(TestClient, email)
    second = _login(first.app, email)

    response = first.post("/api/auth/change-password", json={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD,
    })
    assert response.status_code == 200, response.text
    first.headers["X-CSRF-Token"] = first.cookies.get(CSRF_COOKIE, "")
    assert first.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401

    assert TestClient(first.app, headers=H).post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    ).status_code == 401
    assert _login(first.app, email, NEW_PASSWORD).get("/api/auth/me").status_code == 200


def test_inactive_user_cannot_reuse_a_valid_token(client):
    email = "disabled@example.com"
    c = make_user(TestClient, email)
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        user.is_active = False
        db.commit()
    assert c.get("/api/auth/me").status_code == 401


def test_successful_login_rehashes_when_parameters_change(monkeypatch, client):
    email = "rehash@example.com"
    make_user(TestClient, email)
    import app.routers.auth as auth

    seen: list[str | None] = []
    original = auth.password_needs_rehash

    def spy(hashed):
        seen.append(hashed)
        return original(hashed)

    monkeypatch.setattr(auth, "password_needs_rehash", spy)
    _login(client.app, email)
    assert seen and seen[0]
    assert password_needs_rehash(seen[0]) is False


# --- second review: proxy parsing, durable state, limiter semantics ------------

def test_client_ip_ignores_spoofed_left_forwarded_entries(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # The leftmost entry is client-controlled; the rightmost is appended by the
    # trusted proxy and must win.
    assert client_ip(_request(host="10.0.0.5", forwarded="1.2.3.4, 203.0.113.9")) == "203.0.113.9"


def test_client_ip_counts_trusted_hops_from_the_right(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    request = _request(host="10.0.0.5", forwarded="198.51.100.1, 203.0.113.9, 192.0.2.7")
    assert client_ip(request) == "203.0.113.9"


def test_client_ip_groups_ipv6_by_64(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    same_a = client_ip(_request(forwarded="2001:db8:abcd:1::1234"))
    same_b = client_ip(_request(forwarded="2001:db8:abcd:1::9999"))
    other = client_ip(_request(forwarded="2001:db8:abcd:2::1"))
    assert same_a == same_b == "2001:db8:abcd:1::"
    assert other == "2001:db8:abcd:2::"


def test_client_ip_ignores_garbage_and_oversized_forwarded_headers(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert client_ip(_request(host="198.51.100.10", forwarded="not-an-ip")) == "198.51.100.10"
    assert client_ip(_request(host="198.51.100.10",
                              forwarded="1.2.3.4," * 100)) == "198.51.100.10"


def test_client_ip_uses_socket_peer_when_proxy_untrusted(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    request = _request(host="198.51.100.10", forwarded="1.2.3.4")
    assert client_ip(request) == "198.51.100.10"


def test_db_rate_limit_is_shared_across_sessions_and_returns_retry_after(client):
    key = f"unit:{secrets.token_hex(8)}"
    rate_limit(key, 1, 60)
    with pytest.raises(HTTPException) as exc:  # a different worker/session sees the same hit
        rate_limit(key, 1, 60)
    assert exc.value.status_code == 429
    assert exc.value.headers and exc.value.headers["Retry-After"].isdigit()
    with SessionLocal() as db:
        rows = db.query(RateLimitHit).all()
        assert len(rows) == 1
        assert len(rows[0].bucket) == 64  # SHA-256 digest, never the raw key
        assert key not in rows[0].bucket


def test_login_limits_are_composite_and_return_retry_after(client):
    last = None
    for i in range(21):
        last = TestClient(client.app, headers=H).post(
            "/api/auth/login",
            json={"email": f"nobody{i}@example.com", "password": "Whatever123"},
        )
        if i < 20:
            assert last.status_code == 401, (i, last.text)
    assert last.status_code == 429  # 21st from the same IP, each account still fresh
    assert last.headers.get("Retry-After", "").isdigit()


def test_logout_bumps_version_and_revokes_every_device(client):
    email = "logout@example.com"
    a = make_user(TestClient, email)
    b = _login(a.app, email)
    assert b.get("/api/auth/me").status_code == 200

    assert a.post("/api/auth/logout").status_code == 204
    assert a.get("/api/auth/me").status_code == 401
    assert b.get("/api/auth/me").status_code == 401


def test_tokens_are_signed_with_the_configured_key_mapping(monkeypatch, client):
    custom_key = secrets.token_urlsafe(32)
    monkeypatch.setattr(type(settings), "jwt_keys", PropertyMock(return_value={"primary": custom_key}))
    token = create_token(1, 0, "csrf")
    payload = jwt.decode(token, custom_key, algorithms=["HS256"],
                         issuer=settings.jwt_issuer, audience=settings.jwt_audience)
    assert payload["sub"] == "1"


def test_jwt_leeway_allows_clock_skew_but_rejects_real_expiry(client):
    now = datetime.now(timezone.utc)
    skewed = {**_token_payload(), "iat": now - timedelta(minutes=1),
              "exp": now - timedelta(seconds=5)}
    stale = {**_token_payload(), "iat": now - timedelta(minutes=10),
             "exp": now - timedelta(seconds=60)}
    for payload, expected in ((skewed, True), (stale, False)):
        token = jwt.encode(payload, settings.secret_key, algorithm="HS256",
                           headers={"kid": settings.jwt_key_id})
        assert (decode_token(token) is not None) is expected


def test_config_rejects_colliding_key_id_and_empty_issuer(client):
    with pytest.raises(RuntimeError, match="JWT_KEY_ID"):
        Settings(app_env="development", jwt_key_id="previous-1",
                 jwt_previous_secret_keys="a" * 40).assert_safe()
    with pytest.raises(RuntimeError, match="JWT_ISSUER"):
        Settings(app_env="development", jwt_issuer="").assert_safe()


def test_no_process_local_revocation_or_rate_limit_state(client):
    assert not hasattr(security, "_revoked")
    assert not hasattr(security, "_hits")


# --- third review: header parsing, atomic buckets, bounded hashing -------------

def test_client_ip_joins_all_forwarded_header_lines(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    request = _request(host="10.0.0.5", forwarded=["1.2.3.4", "203.0.113.9"])
    assert client_ip(request) == "203.0.113.9"


def test_client_ip_never_trusts_x_real_ip(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # A garbage XFF line must fall back to the socket peer, not to X-Real-IP.
    assert client_ip(_request(host="198.51.100.10", forwarded="garbage",
                              real_ip="1.2.3.4")) == "198.51.100.10"
    # X-Real-IP is ignored even when there is no XFF header at all.
    assert client_ip(_request(host="198.51.100.10", real_ip="1.2.3.4")) == "198.51.100.10"


def test_client_ip_unwraps_ipv4_mapped_ipv6(monkeypatch, client):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert client_ip(_request(forwarded="::ffff:203.0.113.5")) == "203.0.113.5"
    assert client_ip(_request(forwarded="2001:db8:abcd:1::1234")) == "2001:db8:abcd:1::"


def test_db_rate_limit_does_not_commit_caller_session(client):
    email = "limiter-session@example.com"
    make_user(TestClient, email)
    key = f"session:{secrets.token_hex(8)}"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        user.name = "Uncommitted change"
        rate_limit(key, 1, 60)
        assert db.dirty  # the pending edit still belongs to the caller
        db.rollback()
    with SessionLocal() as db:
        persisted = db.query(User).filter(User.email == email).one()
        assert persisted.name == "T"


def test_db_rate_limit_is_atomic_under_concurrent_requests(client):
    key = f"concurrent:{secrets.token_hex(8)}"
    barrier = threading.Barrier(8)

    def attempt(_):
        barrier.wait()
        try:
            rate_limit(key, 1, 60)
            return "allowed"
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("allowed") == 1
    assert results.count(429) == 7
    with SessionLocal() as db:
        hits = db.query(RateLimitHit).filter(
            RateLimitHit.bucket == fingerprint(key)
        ).count()
    assert hits == 1


def test_fingerprint_is_keyed_hmac_and_not_double_hashed(monkeypatch, client):
    value = "User@Example.com "
    digest = fingerprint(value)
    plain = hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert digest != plain
    original = settings.rate_limit_secret
    monkeypatch.setattr(settings, "rate_limit_secret", original + "x")
    assert fingerprint(value) != digest


def test_argon2_slot_timeout_fails_fast(monkeypatch, client):
    class Busy:
        def acquire(self, timeout=None):
            return False

        def release(self):
            raise AssertionError("a slot that was never acquired must not be released")

    monkeypatch.setattr(security, "_argon2_slots", Busy())
    with pytest.raises(HTTPException) as exc:
        hash_password("Sup3rSecret99")
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException):
        verify_password("Sup3rSecret99", None)


def test_corrupt_password_hash_still_pays_for_a_verification(monkeypatch, client):
    calls: list[str] = []

    def fake_verify(stored_hash, pw):
        calls.append(stored_hash)
        if stored_hash != security._DUMMY_HASH:
            raise InvalidHashError("corrupt stored hash")
        return False

    monkeypatch.setattr(security, "_verify_with_slot", fake_verify)
    assert verify_password("Sup3rSecret99", "$argon2-corrupt") is False
    assert calls == ["$argon2-corrupt", security._DUMMY_HASH]


def test_decode_cannot_disable_expiry_or_required_claims(client):
    params = inspect.signature(security._decode).parameters
    assert "verify_exp" not in params
    assert "require_all" not in params


def test_retry_after_normalises_naive_and_aware_timestamps(client):
    now = datetime(2026, 1, 1, 0, 0, 0)  # naive UTC, as stored in the DB
    oldest_naive = now - timedelta(seconds=30)
    oldest_aware = oldest_naive.replace(tzinfo=timezone.utc)
    assert _retry_after(now, oldest_naive, 60) == 30
    assert _retry_after(now, oldest_aware, 60) == 30


def test_rate_limit_secret_and_rotation_keys_are_validated(client):
    with pytest.raises(RuntimeError, match="RATE_LIMIT_SECRET"):
        Settings(app_env="development", rate_limit_secret="short").assert_safe()
    with pytest.raises(RuntimeError, match="previous-1"):
        Settings(app_env="development",
                 jwt_previous_secret_keys="change-me-previous-key").assert_safe()


def test_external_secrets_file_loads_without_overriding_real_environment(tmp_path, monkeypatch):
    secrets_file = tmp_path / "handoff-secrets.env"
    secrets_file.write_text(
        "SECRET_KEY=file-signing-key\n"
        "RATE_LIMIT_SECRET=file-rate-limit-key\n"
        "DATABASE_URL=sqlite:///from-file.db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMARTBILL_SECRETS_FILE", str(secrets_file))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("RATE_LIMIT_SECRET", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///from-environment.db")

    config._load_external_secrets()

    assert os.environ["SECRET_KEY"] == "file-signing-key"
    assert os.environ["RATE_LIMIT_SECRET"] == "file-rate-limit-key"
    # A shell value is explicit and must win over the file.
    assert os.environ["DATABASE_URL"] == "sqlite:///from-environment.db"


def test_unknown_client_ip_gets_a_stricter_login_limit(monkeypatch, client):
    import app.routers.auth as auth

    monkeypatch.setattr(auth, "client_ip", lambda request: "unknown")
    statuses = []
    for index in range(4):
        response = TestClient(client.app, headers=H).post(
            "/api/auth/login",
            json={"email": f"unknown-{index}@example.com", "password": "Wrong12345"},
        )
        statuses.append(response.status_code)
    assert statuses == [401, 401, 401, 429]

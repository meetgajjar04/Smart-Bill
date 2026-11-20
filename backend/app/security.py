import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, is_sqlite
from .models import RateLimitHit, RateLimitLock, as_utc, utcnow

log = logging.getLogger("smartbill.security")
_ph = PasswordHasher()  # Argon2id, library defaults (~64 MiB per hash)
_DUMMY_HASH = _ph.hash("dummy-password-for-timing-equalisation")
CSRF_COOKIE = "csrf_token"
_REQUIRED_CLAIMS = ("exp", "iat", "sub", "iss", "aud", "jti", "ver", "csrf")

# Argon2 is CPU/memory heavy. Auth routes are sync (FastAPI runs them in its
# threadpool); this cap keeps a burst of logins from exhausting RAM. Acquiring
# a slot has a timeout so a flood of logins cannot park every threadpool thread.
_ARGON2_SLOT_TIMEOUT = 3.0
_argon2_slots = BoundedSemaphore(4)

# Rate-limit rows are shared through the database. MySQL serialises a bucket with
# SELECT ... FOR UPDATE; SQLite (tests/demo, one process) uses a short-lived
# per-bucket lock. Only the lock is in memory -- the counters stay in the DB.
_local_bucket_locks: dict[str, threading.Lock] = {}
_local_bucket_guard = threading.Lock()
_RETENTION_FLOOR_SECONDS = 24 * 60 * 60
_max_window_seen = _RETENTION_FLOOR_SECONDS
_max_window_guard = threading.Lock()


def new_csrf_token() -> str:
    """Opaque value shared by the JWT claim and a readable, host-only cookie."""
    return secrets.token_urlsafe(24)


def _acquire_argon2_slot() -> None:
    if not _argon2_slots.acquire(timeout=_ARGON2_SLOT_TIMEOUT):
        raise HTTPException(503, "Server busy, try again shortly")


def hash_password(pw: str) -> str:
    _acquire_argon2_slot()
    try:
        return _ph.hash(pw)
    finally:
        _argon2_slots.release()


def _verify_with_slot(stored_hash: str, pw: str) -> bool:
    """One Argon2 verification under the concurrency cap."""
    _acquire_argon2_slot()
    try:
        return bool(_ph.verify(stored_hash, pw))
    except VerificationError:
        return False  # normal wrong-password / wrong-user case
    finally:
        _argon2_slots.release()


def verify_password(pw: str, hashed: str | None) -> bool:
    """Always performs one Argon2 verification so unknown users cost the same time.

    A missing OR corrupt stored hash is verified against the dummy hash too, so
    the timing does not reveal whether the account exists or has valid data.
    """
    target = hashed if hashed else _DUMMY_HASH
    try:
        ok = _verify_with_slot(target, pw)
    except (InvalidHashError, ValueError, TypeError):
        # A malformed stored hash is a data-integrity problem, not a bad login.
        log.warning("invalid stored password hash; treat as a data-integrity alert")
        _verify_with_slot(_DUMMY_HASH, pw)
        return False
    return bool(ok and hashed)


def password_needs_rehash(hashed: str | None) -> bool:
    """True when Argon2 parameters have changed and the stored hash should be upgraded."""
    if not hashed:
        return False
    try:
        return bool(_ph.check_needs_rehash(hashed))
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    version: int
    csrf: str
    jti: str


def _decode(token: str) -> dict | None:
    """Decode an HS256 token with pinned algorithm, issuer/audience and key id.

    Every claim is required and expiry is always checked. There are deliberately
    no flags to relax either rule: a future caller must not turn this into an
    authentication bypass by disabling verification.
    """
    try:
        header = jwt.get_unverified_header(token)
        key = settings.jwt_keys.get(header.get("kid", settings.jwt_key_id))
        if not key:
            return None
        return jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=10,
            options={
                "require": list(_REQUIRED_CLAIMS),
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "verify_signature": True,
            },
        )
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        return None


def create_token(user_id: int, token_version: int, csrf: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": secrets.token_urlsafe(12),
        "ver": int(token_version),
        "csrf": csrf,
    }
    # One source of truth: the same mapping used to verify tokens.
    signing_key = settings.jwt_keys[settings.jwt_key_id]
    return jwt.encode(payload, signing_key, algorithm="HS256",
                      headers={"kid": settings.jwt_key_id})


def decode_token(token: str | None) -> TokenClaims | None:
    if not token:
        return None
    data = _decode(token)
    if data is None:
        return None
    try:
        version = int(data["ver"])
        if version < 0 or not str(data["csrf"]):
            return None
        return TokenClaims(int(data["sub"]), version, str(data["csrf"]), str(data["jti"]))
    except (ValueError, TypeError, KeyError):
        return None


def safe_equal(a: str | bytes, b: str | bytes) -> bool:
    """Constant-time compare that is safe for attacker-controlled non-ASCII text."""
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


def csrf_matches(claims: TokenClaims, cookie_value: str | None, header_value: str | None) -> bool:
    """Signed double-submit: cookie, header and the value signed into the JWT must agree."""
    if not cookie_value or not header_value:
        return False
    return safe_equal(cookie_value, header_value) and safe_equal(header_value, claims.csrf)


def fingerprint(value: str) -> str:
    """Keyed HMAC digest for rate-limit buckets.

    A plain SHA-256 of an email/IP is trivially reversible by brute force; the
    application secret makes the mapping one-way to anyone without that secret.
    This is also the ONLY hashing step: callers pass the logical key here and
    ``rate_limit`` stores the digest directly.
    """
    secret = settings.rate_limit_secret.encode("utf-8")
    return hmac.new(secret, value.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


# --- proxy-aware client address ------------------------------------------------
_MAX_FORWARDED_HEADER = 512
_MAX_FORWARDED_ENTRIES = 20


def _normalise_ip(value: str) -> str:
    try:
        ip = ipaddress.ip_address(value.strip())
    except (ValueError, TypeError):
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        # Dual-stack sockets present IPv4 clients as ::ffff:a.b.c.d. Without
        # this, every IPv4 client would collapse into the same masked /64 bucket.
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        # Mobile/carrier NAT changes the interface within a /64; group it.
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False).network_address)
    return str(ip)


def _header_values(headers, name: str) -> list[str]:
    """Read every line of a header (Starlette) or a plain string/list in tests."""
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return [str(value) for value in getlist(name)]
    value = headers.get(name)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def client_ip(request: Request) -> str:
    """Return the address to rate-limit.

    With ``trust_proxy_headers`` off, use the socket peer only. With it on, join
    every X-Forwarded-For header line, then walk the chain from the RIGHT,
    skipping ``TRUSTED_PROXY_HOPS`` trusted hops (the proxy itself plus any
    upstream proxies). The leftmost entries are client-controlled and are
    deliberately ignored. If any entry is invalid, fall back to the socket peer
    and consult no other address header. X-Real-IP is never trusted.
    """
    remote = _normalise_ip(request.client.host) if request.client else "unknown"
    if not settings.trust_proxy_headers:
        return remote

    chain = [remote]
    lines = _header_values(request.headers, "x-forwarded-for")
    if lines and all(line and len(line) <= _MAX_FORWARDED_HEADER for line in lines):
        joined = ",".join(lines)
        parts = [part.strip() for part in joined.split(",")]
        if 0 < len(parts) <= _MAX_FORWARDED_ENTRIES:
            parsed = [_normalise_ip(part) for part in parts]
            if all(ip != "unknown" for ip in parsed):
                chain = parsed + [remote]

    index = len(chain) - 1 - max(0, settings.trusted_proxy_hops)
    return chain[index] if index >= 0 else remote


# --- durable sliding-window limiter (shared by every worker/process) -----------
def _remember_window(window: int) -> None:
    """Keep the retention floor at least as long as the longest window we've seen."""
    global _max_window_seen
    with _max_window_guard:
        _max_window_seen = max(_max_window_seen, max(0, int(window)))


def _local_lock(bucket: str):
    """SQLite has no FOR UPDATE; serialise buckets inside this process instead."""
    if not is_sqlite:
        return nullcontext()
    with _local_bucket_guard:
        lock = _local_bucket_locks.get(bucket)
        if lock is None:
            lock = threading.Lock()
            _local_bucket_locks[bucket] = lock
        return lock


def _ensure_lock_row(db: Session, bucket: str) -> None:
    """Create the per-bucket row if absent; another worker may win the race."""
    try:
        with db.begin_nested():
            db.add(RateLimitLock(bucket=bucket))
    except IntegrityError:
        pass  # another worker created it; the existing row is what we lock below
    if db.query(RateLimitLock).filter(RateLimitLock.bucket == bucket).with_for_update().first() is None:
        raise RuntimeError(f"rate-limit lock row missing for {bucket[:12]}")


def _retry_after(now: datetime, oldest: datetime | None, window: int) -> int:
    """Retry delay, safe for both naive and timezone-aware DB timestamps."""
    oldest_utc = as_utc(oldest)
    if oldest_utc is None:
        return max(1, window)
    return max(1, math.ceil(window - (now - oldest_utc).total_seconds()))


def rate_limit(key: str, limit: int, window: int) -> None:
    """Atomically count recent hits for ``key`` in the shared database.

    A dedicated short session means a rejected or accepted request never commits
    unrelated work from the caller's Session. MySQL locks one bucket row with
    SELECT ... FOR UPDATE; SQLite uses a process-local lock for the same window.
    """
    _remember_window(window)
    bucket = fingerprint(key)
    now = utcnow()
    cutoff = now - timedelta(seconds=window)

    with _local_lock(bucket):
        with SessionLocal() as db:
            with db.begin():
                _ensure_lock_row(db, bucket)
                recent = db.query(RateLimitHit).filter(
                    RateLimitHit.bucket == bucket, RateLimitHit.occurred_at >= cutoff
                )
                if recent.count() >= limit:
                    oldest = recent.order_by(RateLimitHit.occurred_at).first()
                    retry = _retry_after(now, oldest.occurred_at if oldest else None, window)
                    log.warning("rate limit hit bucket=%s limit=%s window=%s retry=%s",
                                bucket[:12], limit, window, retry)
                    raise HTTPException(
                        429, "Too many attempts. Please wait and try again.",
                        headers={"Retry-After": str(retry)},
                    )

                # Only this bucket's stale hits are removed. A global DELETE on
                # every request caused lock contention and silently broke any
                # window longer than the purge age.
                db.query(RateLimitHit).filter(
                    RateLimitHit.bucket == bucket, RateLimitHit.occurred_at < cutoff
                ).delete(synchronize_session=False)

                # Rare, bounded housekeeping for buckets that are never visited
                # again. Retention always covers the longest window we've seen.
                if secrets.randbelow(100) == 0:
                    retention = now - timedelta(seconds=_max_window_seen)
                    db.query(RateLimitHit).filter(
                        RateLimitHit.occurred_at < retention
                    ).delete(synchronize_session=False)

                db.add(RateLimitHit(bucket=bucket, occurred_at=now))

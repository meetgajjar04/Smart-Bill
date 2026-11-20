import logging
import shutil
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import COOKIE, get_current_user
from ..models import User, as_utc, utcnow
from ..schemas import LoginIn, PasswordChangeIn, ProfileIn, RegisterIn, UserOut
from ..security import (
    CSRF_COOKIE, client_ip, create_token, decode_token, hash_password,
    new_csrf_token, password_needs_rehash, rate_limit, verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("smartbill.auth")
MAX_FAILS, LOCK_MIN = 5, 15


def _set_cookie(resp: Response, user_id: int, token_version: int) -> None:
    csrf = new_csrf_token()
    max_age = settings.access_token_minutes * 60
    resp.set_cookie(COOKIE, create_token(user_id, token_version, csrf), max_age=max_age,
                    httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    # Readable by the SPA only; the matching value is also signed into the JWT.
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
                    secure=settings.cookie_secure, samesite="lax", path="/")


def _clear_cookies(resp: Response) -> None:
    """Delete with the same attributes used when setting, or browsers may keep them."""
    common = dict(path="/", secure=settings.cookie_secure, samesite="lax")
    resp.delete_cookie(COOKIE, httponly=True, **common)
    resp.delete_cookie(CSRF_COOKIE, httponly=False, **common)


def _revoke_everywhere(resp: Response, db: Session, user: User, action: str) -> None:
    """Bump the durable per-user version: every existing token fails the next check."""
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    _clear_cookies(resp)
    log.info("%s user_id=%s version=%s", action, user.id, user.token_version)


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    # No usable peer address is a strong signal of a broken/misconfigured proxy;
    # give that shared bucket a much tighter limit than a real client address.
    rate_limit(f"reg:{ip}", 2 if ip == "unknown" else 5, 300)
    email = data.email.lower()
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(409, "An account with this email already exists.")
    user = User(email=email, name=data.name, password_hash=hash_password(data.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two registrations with the same address can pass the check above together.
        db.rollback()
        raise HTTPException(409, "An account with this email already exists.")
    _set_cookie(response, user.id, user.token_version)
    log.info("register ok user_id=%s", user.id)
    return user


@router.post("/login", response_model=UserOut)
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    # Normalise the key the same way the account is stored. rate_limit applies
    # the keyed HMAC once, so raw addresses/emails are never persisted.
    email = data.email.strip().lower()
    ip = client_ip(request)
    user = db.query(User).filter(User.email == email).first()
    # Check the lockout first: a locked account must report 423, not a generic 429.
    if user and user.locked_until and as_utc(user.locked_until) > utcnow():
        raise HTTPException(423, "Account temporarily locked after repeated failed sign-ins. Try again in a few minutes.")
    # Composite, durable limits: tight per account, looser per client address.
    # Per-IP alone would punish offices/mobile carriers; per-account alone would
    # allow credential stuffing from many addresses. An unknown peer gets a much
    # tighter address bucket instead of a shared, easy-to-exhaust one.
    rate_limit(f"login-account:{email}", 5, 900)
    rate_limit(f"login-ip:{ip}", 3 if ip == "unknown" else 20, 900)
    password_ok = verify_password(data.password, user.password_hash if user and user.is_active else None)
    if not user or not user.is_active or not password_ok:
        if user and user.is_active:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILS:
                user.locked_until, user.failed_logins = utcnow() + timedelta(minutes=LOCK_MIN), 0
            db.commit()
        log.warning("login failed email=%s active=%s", email, bool(user and user.is_active))
        raise HTTPException(401, "Incorrect email or password.")  # same message for both cases
    # Roll forward to stronger Argon2 parameters without making the user change it.
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    _set_cookie(response, user.id, user.token_version)
    log.info("login ok user_id=%s", user.id)
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Clear cookies always; revoke every token if the presented session is valid.

    There is no process-local denylist: the version bump lives in the database,
    so it works with any number of workers and survives a restart. An expired
    token still gets its browser cookies cleared instead of a 401.
    """
    claims = decode_token(request.cookies.get(COOKIE))
    user = db.get(User, claims.user_id) if claims else None
    if user and user.is_active and user.token_version == claims.version:
        _revoke_everywhere(response, db, user, "logout")
    else:
        _clear_cookies(response)


@router.post("/logout-all", status_code=204)
def logout_all(response: Response,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Explicitly invalidate every session, including tokens on other devices."""
    _revoke_everywhere(response, db, user, "logout-all")


@router.post("/change-password", response_model=UserOut)
def change_password(data: PasswordChangeIn, response: Response,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rate_limit(f"password-change:{user.id}", 5, 300)
    if not verify_password(data.current_password, user.password_hash):
        log.warning("password change failed verification user_id=%s", user.id)
        raise HTTPException(401, "Incorrect email or password.")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(400, "Choose a password you are not already using.")
    user.password_hash = hash_password(data.new_password)
    user.token_version = (user.token_version or 0) + 1
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    _set_cookie(response, user.id, user.token_version)
    log.info("password changed user_id=%s version=%s", user.id, user.token_version)
    return user


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(data: ProfileIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if data.name is not None:
        user.name = data.name.strip()
    if data.notify_email is not None:
        user.notify_email = data.notify_email
    db.commit()
    return user


@router.delete("/me", status_code=204)
def delete_me(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Right to erasure: removes the account, every bill, alert and uploaded file."""
    db.delete(user)
    db.commit()  # commit first: a failed commit must not leave files pointing at a live account
    shutil.rmtree(Path(settings.upload_dir).resolve() / str(user.id), ignore_errors=True)
    # No denylist needed: the deleted user cannot resolve on the next request.
    _clear_cookies(response)
    log.info("account deleted user_id=%s", user.id)

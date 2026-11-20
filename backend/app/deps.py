from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User
from .security import CSRF_COOKIE, csrf_matches, decode_token, safe_equal

COOKIE = "access_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-csrf-token"
REQUESTED_WITH = "SmartBillAI"


def verify_csrf(request: Request) -> None:
    """One dependency for every state-changing route (installed on all routers).

    * A custom header blocks cross-site HTML forms/images.
    * When a session cookie exists, the readable CSRF cookie and the value signed
      into the JWT must both match the header (double-submit).
    * Login/register have no cookie yet; the Origin check in ``main.py`` covers them.
    """
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("x-requested-with") != REQUESTED_WITH:
        raise HTTPException(403, "Missing CSRF header.")
    claims = decode_token(request.cookies.get(COOKIE))
    if claims and not csrf_matches(
        claims, request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)
    ):
        raise HTTPException(403, "Invalid CSRF token.")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    claims = decode_token(request.cookies.get(COOKIE))
    user = db.get(User, claims.user_id) if claims else None
    # A deleted, disabled or version-bumped user must not be able to reuse a token.
    if not user or not user.is_active or claims is None or claims.version != user.token_version:
        raise HTTPException(401, "Please sign in.")
    return user


def require_internal(x_internal_key: str = Header(default="")) -> None:
    """Service-to-service auth for n8n -> backend calls."""
    if not x_internal_key or not safe_equal(x_internal_key, settings.internal_api_key):
        raise HTTPException(401, "Invalid internal key.")

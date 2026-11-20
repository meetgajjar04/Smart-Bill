"""E-mail configuration status and a test send, used by the Settings page."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..security import rate_limit
from ..services import emailer

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/status")
def email_status(user: User = Depends(get_current_user)):
    return {
        "enabled": bool(settings.email_enabled),
        "configured": bool(settings.smtp_ready),
        "host": settings.smtp_host or None,
        "from": (settings.smtp_from or settings.smtp_username) or None,
        "reminder_days_before": settings.reminder_days_before,
        "preference_on": bool(user.notify_email),
    }


@router.post("/test")
def send_test_email(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rate_limit(f"email-test:{user.id}", 3, 300)
    if not settings.smtp_ready:
        raise HTTPException(503, "E-mail delivery is not configured. Set SMTP_HOST, SMTP_USERNAME, "
                                 "SMTP_PASSWORD and SMTP_FROM in .env, then restart the backend.")
    if not emailer.send_test(user):
        raise HTTPException(502, "The SMTP server refused the message. Check the server log for the exact error.")
    return {"sent": True, "to": user.email}

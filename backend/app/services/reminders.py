"""Deadline reminders, run by the backend itself (no n8n required).

Every hour the app looks for reviewed, unpaid bills due `REMINDER_DAYS_BEFORE` days
from today (default 1) and e-mails the owner. Successful sends are recorded in
`Bill.reminded_at`, and an in-app alert is created once per bill per day.
"""
import asyncio
import logging
from datetime import date, timedelta
from threading import Lock
from types import SimpleNamespace

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import Bill, User, as_utc, utcnow
from . import alerting, emailer

log = logging.getLogger("smartbill.reminders")
DEDUPE = timedelta(hours=20)
# One process may have several workers/timers. Do not let two runs claim the same bill.
_RUN_LOCK = Lock()


def _bill_view(bill: Bill):
    """Detached snapshot so SMTP never holds a DB transaction open."""
    return SimpleNamespace(
        id=bill.id, user_id=bill.user_id, category=bill.category, provider=bill.provider,
        account_number=bill.account_number, amount=bill.amount, due_date=bill.due_date,
    )


def _user_view(user: User):
    return SimpleNamespace(id=user.id, email=user.email, name=user.name, notify_email=user.notify_email)


def _amount(bill) -> str:
    return f"₹{float(bill.amount):,.0f}" if bill.amount is not None else "amount not recorded"


def _recently_alerted(db: Session, bill_id: int, cutoff) -> bool:
    return alerting.recently_created(db, bill_id, "reminder", cutoff)


def send_due_reminders(db: Session, days_before: int | None = None) -> dict:
    """Send e-mails for bills due in `days_before` days. Safe to call repeatedly."""
    with _RUN_LOCK:
        lead = settings.reminder_days_before if days_before is None else days_before
        target = date.today() + timedelta(days=lead)
        cutoff = utcnow() - DEDUPE
        # Include recently reminded bills so a repeat run can report them as
        # skipped; the atomic claim below decides who actually sends.
        rows = (db.query(Bill, User).join(User, User.id == Bill.user_id)
                .filter(Bill.reviewed.is_(True), Bill.paid.is_(False),
                        Bill.amount.isnot(None), Bill.due_date == target,
                        User.notify_email.is_(True)).all())

        result = {"checked": len(rows), "sent": 0, "in_app": 0, "failed": 0, "skipped": 0,
                  "target": target.isoformat()}
        for bill, user in rows:
            bill_id, bill_view, user_view = bill.id, _bill_view(bill), _user_view(user)
            prior = as_utc(bill.reminded_at)
            now = utcnow()

            # Atomic claim inside the 20h window: a parallel worker that got here
            # first simply loses this bill instead of sending a second e-mail.
            claimed = (db.query(Bill)
                       .filter(Bill.id == bill_id,
                               or_(Bill.reminded_at.is_(None), Bill.reminded_at < cutoff))
                       .update({Bill.reminded_at: now}, synchronize_session=False))
            db.commit()
            if not claimed:
                result["skipped"] += 1
                continue

            when = "tomorrow" if lead == 1 else f"in {lead} days"
            alerting.add_alert(
                db, user_id=user.id, bill_id=bill_id, kind="reminder", severity="warning",
                message=(f"{bill.provider or bill.category.title()} bill of {_amount(bill_view)} "
                         f"is due {when} ({bill.due_date.strftime('%d %b %Y')})."),
            )

            delivered = emailer.send_due_reminder(user_view, bill_view)
            if delivered or not settings.smtp_ready:
                # Without SMTP the in-app alert is the delivery, so keep the
                # dedupe window closed either way.
                result["sent" if delivered else "in_app"] += 1
                continue

            # SMTP failed: let the next hourly run try again. The in-app alert
            # stays, and its dedupe key prevents a duplicate the same day.
            (db.query(Bill).filter(Bill.id == bill_id, Bill.reminded_at == now)
             .update({Bill.reminded_at: prior}, synchronize_session=False))
            db.commit()
            result["failed"] += 1

        if result["sent"] or result["failed"]:
            log.info("deadline reminders: %s", result)
        return result


async def reminder_loop(interval_seconds: int = 3600) -> None:
    """Background loop started from the app lifespan when e-mail is configured."""
    await asyncio.sleep(5)  # let startup finish first
    while True:
        try:
            with SessionLocal() as db:
                send_due_reminders(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("deadline reminder run failed")
        await asyncio.sleep(interval_seconds)

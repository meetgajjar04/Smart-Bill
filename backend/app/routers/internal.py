"""Service-to-service API used by the n8n workflows. Protected by X-Internal-Key and never exposed by nginx."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_internal
from ..models import Bill, User, as_utc, utcnow
from ..schemas import AnalysisResultIn, InternalAlertIn
from ..services import alerting, analysis, reminders

router = APIRouter(prefix="/internal", tags=["internal (n8n)"], dependencies=[Depends(require_internal)])


def _bill(db: Session, bill_id: int) -> Bill:
    b = db.get(Bill, bill_id)
    if not b or not b.reviewed:
        raise HTTPException(404, "Bill not found.")
    return b


def _amount(bill: Bill) -> str:
    """Legacy rows can have a NULL amount; never let that turn into a 500."""
    return f"₹{float(bill.amount):,.0f}" if bill.amount is not None else "amount not recorded"


@router.get("/bills/{bill_id}/analysis")
def get_analysis(bill_id: int, db: Session = Depends(get_db)):
    return analysis.analyse_bill(db, _bill(db, bill_id))


@router.post("/bills/{bill_id}/analysis-result", status_code=204)
def save_result(bill_id: int, data: AnalysisResultIn, db: Session = Depends(get_db)):
    analysis.persist_result(db, _bill(db, bill_id), data.model_dump(), create_alert=False)


@router.post("/alerts", status_code=201)
def create_alert(data: InternalAlertIn, db: Session = Depends(get_db)):
    if not db.get(User, data.user_id):
        raise HTTPException(404, "User not found.")
    if data.bill_id is not None:
        bill = db.get(Bill, data.bill_id)
        if not bill or not bill.reviewed or bill.user_id != data.user_id:
            # Do not let a workflow attach another customer's bill to this alert.
            raise HTTPException(404, "Bill not found.")
    created = alerting.add_alert(
        db, user_id=data.user_id, bill_id=data.bill_id, kind=data.kind,
        severity=data.severity, message=data.message,
    )
    return {"created": created}


@router.get("/reminders/due")
def due_reminders(days: int = Query(3, ge=0, le=30), db: Session = Depends(get_db)):
    """Unpaid bills due within `days` (or already overdue), not reminded recently.

    Opted-out users and rows missing an amount or due date are excluded here too,
    so every caller of this endpoint gets an actionable list.
    """
    today, cutoff = date.today(), utcnow() - timedelta(hours=20)
    rows = (db.query(Bill, User).join(User, User.id == Bill.user_id)
            .filter(Bill.reviewed.is_(True), Bill.paid.is_(False),
                    Bill.amount.isnot(None), Bill.due_date.isnot(None),
                    Bill.due_date <= today + timedelta(days=days),
                    User.notify_email.is_(True),
                    or_(Bill.reminded_at.is_(None), Bill.reminded_at < cutoff)).all())
    return {"reminders": [{
        "bill_id": b.id, "user_id": u.id, "name": u.name, "email": u.email, "notify_email": u.notify_email,
        "provider": b.provider or b.category.title(), "category": b.category, "amount": float(b.amount),
        "due_date": b.due_date.isoformat(), "days_left": (b.due_date - today).days,
    } for b, u in rows]}


@router.post("/reminders/run")
def run_reminders(days: int = Query(1, ge=0, le=14), db: Session = Depends(get_db)):
    """Send deadline e-mails now (also used by cron/manual operations)."""
    return reminders.send_due_reminders(db, days_before=days)


@router.post("/reminders/{bill_id}/sent", status_code=204)
def reminder_sent(bill_id: int, db: Session = Depends(get_db)):
    """Record that a workflow sent a reminder. Repeated calls inside 20h do nothing."""
    bill = _bill(db, bill_id)
    if bill.due_date is None:
        raise HTTPException(422, "Bill has no due date.")
    cutoff = utcnow() - timedelta(hours=20)
    if bill.reminded_at and as_utc(bill.reminded_at) >= cutoff:
        return

    left = (bill.due_date - date.today()).days
    when = "is overdue" if left < 0 else "is due today" if left == 0 else f"is due in {left} day(s)"
    bill.reminded_at = utcnow()
    db.commit()  # close the dedupe window before writing the alert
    alerting.add_alert(
        db, user_id=bill.user_id, bill_id=bill.id, kind="reminder",
        severity="critical" if left < 0 else "warning",
        message=f"{bill.provider or bill.category.title()} bill of {_amount(bill)} {when}.",
    )

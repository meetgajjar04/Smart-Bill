"""Glue: build analysis context, dispatch to n8n, fall back to local analysis, persist insights."""
import logging
from datetime import date

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import Bill, Insight
from . import analysis

log = logging.getLogger("smartbill.pipeline")
ALL_TYPES = {"anomaly", "deadline", "prediction", "rate_change", "recommendation"}


def build_context(db: Session, bill: Bill) -> dict:
    rows = (db.query(Bill.bill_date, Bill.amount, Bill.units)
            .filter(Bill.user_id == bill.user_id, Bill.category == bill.category, Bill.verified.is_(True),
                    Bill.bill_date.isnot(None), Bill.amount.isnot(None)).all())
    series = analysis.monthly_series([(d, float(a), u) for d, a, u in rows])
    cur_p = bill.bill_date.strftime("%Y-%m")
    past = [s for s in series if s["period"] < cur_p]
    upto = [s for s in series if s["period"] <= cur_p]
    cur = next((s for s in series if s["period"] == cur_p), {"amount": float(bill.amount), "units": bill.units})
    return {
        "today": date.today().isoformat(),
        "bill": {"id": bill.id, "user_id": bill.user_id, "category": bill.category, "provider": bill.provider,
                 "amount": float(bill.amount), "bill_date": bill.bill_date.isoformat(),
                 "due_date": bill.due_date.isoformat() if bill.due_date else None,
                 "units": bill.units, "is_paid": bill.is_paid},
        "period_total": cur["amount"], "period_units": cur["units"],
        "history": past[-24:],
        "prediction": analysis.predict_next([s["amount"] for s in upto], [s["period"] for s in upto]),
        "anomaly": analysis.detect_anomaly([s["amount"] for s in past], cur["amount"]),
    }


def save_insights(db: Session, bill: Bill, insights: list[dict], replace: set[str] | None = None) -> None:
    replace = replace or ALL_TYPES
    db.query(Insight).filter(Insight.bill_id == bill.id, Insight.type.in_(replace)).delete(synchronize_session=False)
    for i in insights:
        db.add(Insight(user_id=bill.user_id, bill_id=bill.id, type=i["type"], severity=i["severity"],
                       title=i["title"][:200], message=i["message"][:1000], data=i.get("data") or {}))
    db.commit()


def run_local(bill_id: int) -> None:
    with SessionLocal() as db:
        bill = db.get(Bill, bill_id)
        if bill and bill.verified and bill.amount and bill.bill_date:
            save_insights(db, bill, analysis.build_insights(build_context(db, bill)))


def dispatch_analysis(bill_id: int) -> None:
    """Background task: ask n8n to analyse the bill; if n8n is down/unconfigured analyse locally."""
    if settings.n8n_webhook_url:
        try:
            with SessionLocal() as db:
                bill = db.get(Bill, bill_id)
                payload = {"bill_id": bill_id, "user_id": bill.user_id}
            r = httpx.post(settings.n8n_webhook_url, json=payload, timeout=30,
                           headers={"X-Webhook-Secret": settings.n8n_webhook_secret})
            r.raise_for_status()
            return
        except Exception as e:  # network error, 4xx/5xx, workflow inactive...
            log.warning("n8n unavailable (%s) - using local analysis", e)
    try:
        run_local(bill_id)
    except Exception:
        log.exception("Local analysis failed for bill %s", bill_id)

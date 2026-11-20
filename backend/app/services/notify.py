"""Hands a confirmed bill to n8n. If n8n is down/unconfigured we run the same analysis locally,
so the app never silently loses an analysis (reliability by graceful degradation)."""
import logging

import httpx

from ..config import settings
from ..database import SessionLocal
from ..models import Bill
from . import analysis

log = logging.getLogger("smartbill.notify")


def dispatch_analysis(bill_id: int) -> None:
    """Hand a confirmed bill to n8n, falling back to local analysis when needed.

    The first session only reads the immutable identity of the bill and is closed
    before the webhook call: a slow n8n must not hold a connection or transaction
    open. The local fallback opens its own short-lived session to analyse and
    persist, so the result is never lost silently.
    """
    with SessionLocal() as db:
        bill = db.get(Bill, bill_id)
        if not bill or not bill.reviewed:
            return
        payload = {"bill_id": bill.id, "user_id": bill.user_id}

    if settings.n8n_webhook_url:
        try:
            r = httpx.post(
                settings.n8n_webhook_url,
                json=payload,
                headers={"X-Webhook-Secret": settings.n8n_webhook_secret},
                timeout=4.0,
            )
            r.raise_for_status()
            return  # n8n will call back /internal/bills/{id}/analysis-result
        except Exception as e:
            log.warning("n8n unavailable (%s) — running local analysis", e)
    else:
        log.info("n8n is not configured; running local analysis for bill %s", bill_id)

    try:
        with SessionLocal() as db:
            bill = db.get(Bill, bill_id)
            if not bill or not bill.reviewed:
                log.info("bill %s was removed or unreviewed before local analysis", bill_id)
                return
            result = analysis.local_verdict(analysis.analyse_bill(db, bill))
            analysis.persist_result(db, bill, result)
    except Exception:
        log.exception("local analysis failed for bill %s", bill_id)

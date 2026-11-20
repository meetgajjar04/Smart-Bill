"""Small cache for Ollama output, so the UI can show the last analysis without re-running the model."""
import threading
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AIInsight

# A save is clear + insert, not an upsert: two concurrent requests must not leave
# two current rows for the same (user, kind, bill). One process, one lock.
_SAVE_LOCK = threading.Lock()


def latest(db: Session, user_id: int, kind: str, bill_id: Optional[int] = None) -> Optional[AIInsight]:
    q = db.query(AIInsight).filter(AIInsight.user_id == user_id, AIInsight.kind == kind)
    q = q.filter(AIInsight.bill_id == bill_id) if bill_id is not None else q.filter(AIInsight.bill_id.is_(None))
    return q.order_by(AIInsight.id.desc()).first()


def save(db: Session, user_id: int, kind: str, payload: dict, bill_id: Optional[int] = None,
         model: Optional[str] = None) -> AIInsight:
    """Replace the previous insight of this kind (keeps a single current row).

    ``model`` records where the answer came from: a rule-based fallback must not
    be stored under the configured model's name. When omitted, the configured
    model is still used (existing callers keep their behaviour).
    """
    with _SAVE_LOCK:
        clear(db, user_id, bill_id, kind)
        row = AIInsight(user_id=user_id, bill_id=bill_id, kind=kind,
                        model=model or (settings.ollama_analysis_model or settings.ollama_model
                                        or "smart-bill-ai"),
                        payload=payload)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def clear(db: Session, user_id: int, bill_id: Optional[int] = None, kind: Optional[str] = None) -> None:
    q = db.query(AIInsight).filter(AIInsight.user_id == user_id)
    if bill_id is not None:
        q = q.filter(AIInsight.bill_id == bill_id)
    if kind is not None:
        q = q.filter(AIInsight.kind == kind)
    q.delete(synchronize_session=False)
    db.commit()


def clear_bill(db: Session, user_id: int, bill_id: int) -> None:
    """Only this bill's AI analysis is stale."""
    clear(db, user_id, bill_id)


def invalidate_portfolio(db: Session, user_id: int) -> None:
    """Bills or the home profile changed: the portfolio summary is stale."""
    clear(db, user_id, None, "portfolio")


def invalidate_for_bill(db: Session, user_id: int, bill_id: int) -> None:
    """A bill changed: its own analysis and the portfolio summary are both stale."""
    clear_bill(db, user_id, bill_id)
    invalidate_portfolio(db, user_id)

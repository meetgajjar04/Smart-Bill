"""Shared, idempotent creation of in-app alerts.

A bill-level alert is written at most once per kind per day. The database unique
key is the final guard; the lookup below avoids a needless write. Generic info
alerts have no bill and are intentionally not de-duplicated.
"""
from datetime import date
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Alert, utcnow


def dedupe_key(kind: str, bill_id: Optional[int], day: Optional[date] = None) -> Optional[str]:
    if bill_id is None:
        return None
    return f"{kind}:{bill_id}:{(day or utcnow().date()).isoformat()}"


def recently_created(db: Session, bill_id: int, kind: str, cutoff) -> bool:
    """True when an alert of this kind for the bill exists inside the time window."""
    return bool(db.query(Alert.id).filter(
        Alert.bill_id == bill_id,
        Alert.kind == kind,
        Alert.created_at >= cutoff,
    ).first())


def add_alert(db: Session, *, user_id: int, bill_id: Optional[int], kind: str,
              severity: str, message: str, day: Optional[date] = None) -> bool:
    """Create an alert unless an identical one already exists. Returns True when created."""
    key = dedupe_key(kind, bill_id, day)
    if key and db.query(Alert.id).filter(Alert.user_id == user_id, Alert.dedupe_key == key).first():
        return False
    db.add(Alert(user_id=user_id, bill_id=bill_id, kind=kind, severity=severity,
                 message=message[:500], dedupe_key=key))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent worker won the race; keep the first alert.
        db.rollback()
        return False
    return True

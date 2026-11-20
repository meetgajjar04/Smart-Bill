from datetime import date, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import CATEGORIES, Alert, Bill, User
from ..schemas import AlertOut, BillOut, WhatIfIn
from ..services import analysis

router = APIRouter(tags=["analytics"])


@router.get("/analytics/summary")
def summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    today = date.today()
    this_m, last_m = pd.Period(today, "M"), pd.Period(today, "M") - 1
    df = analysis.monthly(db, user.id)

    def total(p):
        if df.empty or "amount" not in df:
            return 0.0
        return round(float(df[df["month"] == p]["amount"].sum()), 2)

    cur, prev = total(this_m), total(last_m)
    bills = (db.query(Bill).filter(Bill.user_id == user.id, Bill.reviewed.is_(True), Bill.paid.is_(False))
             .order_by(Bill.due_date).all())
    # A legacy bill may have no amount. Keep it in the count and upcoming list,
    # but never try to add a missing value to the money total.
    known_amounts = [float(b.amount) for b in bills if b.amount is not None]
    overdue = [b for b in bills if b.due_date and b.due_date < today]
    soon = [b for b in bills if b.due_date and today <= b.due_date <= today + timedelta(days=7)]

    months = pd.period_range(this_m - 11, this_m, freq="M")
    trend = []
    for m in months:
        row = {"month": str(m)}
        for c in CATEGORIES:
            row[c] = round(float(df[(df["month"] == m) & (df["category"] == c)]["amount"].sum()), 2) if not df.empty else 0.0
        trend.append(row)
    share = {c: round(sum(r[c] for r in trend), 2) for c in CATEGORIES}

    return {
        "this_month": cur, "last_month": prev,
        "change_pct": round((cur - prev) / prev * 100, 1) if prev else None,
        "unpaid_count": len(bills), "unpaid_amount": round(sum(known_amounts), 2),
        "overdue_count": len(overdue), "due_soon_count": len(soon),
        "draft_count": db.query(Bill).filter(Bill.user_id == user.id, Bill.reviewed.is_(False)).count(),
        "upcoming": [BillOut.model_validate(b).model_dump(mode="json") for b in bills[:8]],
        "trend": trend, "share": share,
    }


@router.get("/analytics/predict")
def predict(category: str = Query("electricity"), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if category not in CATEGORIES:
        raise HTTPException(422, "Unknown category.")
    return analysis.forecast_for(db, user.id, category)


@router.get("/analytics/anomalies")
def anomalies(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return analysis.anomaly_scan(db, user.id)


@router.get("/analytics/recommendations")
def recs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return analysis.recommendations(db, user.id)


@router.get("/alerts", response_model=list[AlertOut])
def alerts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Alert).filter(Alert.user_id == user.id, Alert.is_read.is_(False)).order_by(Alert.id.desc()).limit(30).all()


@router.post("/alerts/{alert_id}/read", status_code=204)
def mark_read(alert_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Alert not found.")
    a.is_read = True
    db.commit()


@router.post("/analytics/what-if")
def what_if(data: WhatIfIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    changes = data.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Change at least one setting.")
    try:
        return analysis.what_if(db, user.id, changes)
    except ValueError as e:
        raise HTTPException(409, str(e))

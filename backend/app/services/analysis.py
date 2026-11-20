"""Glue between the database and the ML/recommendation modules (Pandas does the aggregation)."""
import logging
from datetime import date

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Bill, HouseholdProfile, User
from . import alerting, ml, profile_model as pm
from .recommend import recommend

log = logging.getLogger("smartbill.analysis")


def _rows(db: Session, user_id: int, category: str | None = None) -> pd.DataFrame:
    q = db.query(Bill).filter(Bill.user_id == user_id, Bill.reviewed.is_(True), Bill.amount.isnot(None))
    if category:
        q = q.filter(Bill.category == category)
    data = [
        {"category": b.category, "amount": float(b.amount), "units": b.units, "month": pd.Period(b.period, "M"),
         "due": b.due_date, "paid_on": b.paid_on, "paid": b.paid, "kind": (b.feedback or {}).get("kind", "none")}
        for b in q if b.period
    ]
    return pd.DataFrame(data, columns=["category", "amount", "units", "month", "due", "paid_on", "paid", "kind"])


def monthly(db: Session, user_id: int, category: str | None = None) -> pd.DataFrame:
    df = _rows(db, user_id, category)
    if df.empty:
        return pd.DataFrame(columns=["month", "category", "amount"])
    return df.groupby(["month", "category"], as_index=False)["amount"].sum().sort_values("month")


def category_series(db: Session, user_id: int, category: str, clean: bool = False) -> pd.DataFrame:
    """Monthly totals. clean=True applies what the user told us about unusual bills:
       'temporary' causes (guests, travel, meter fault) are dropped; a 'permanent' change (new appliance,
       tariff change) makes the baseline start from that month."""
    df = _rows(db, user_id, category)
    if df.empty:
        return pd.DataFrame({"month": pd.Series(dtype="object"), "amount": pd.Series(dtype=float)})
    g = df.groupby("month").agg(amount=("amount", "sum"), temp=("kind", lambda k: (k == "temporary").any()),
                                perm=("kind", lambda k: (k == "permanent").any())).reset_index().sort_values("month")
    if clean:
        g = g[~g["temp"]]
        perm = g[g["perm"]]["month"]
        if len(perm):
            post = g[g["month"] >= perm.max()]
            if len(post) >= 2:
                g = post
    return g[["month", "amount"]].reset_index(drop=True)


def all_series(db: Session, user_id: int) -> dict[str, list[float]]:
    out = {}
    for cat in set(monthly(db, user_id)["category"]):
        out[cat] = category_series(db, user_id, cat, clean=True)["amount"].tolist()
    return out


def late_count(db: Session, user_id: int) -> int:
    df = _rows(db, user_id)
    if df.empty:
        return 0
    paid_late = df[(df.paid) & df.paid_on.notna() & df.due.notna()].apply(lambda r: r.paid_on > r.due, axis=1).sum()
    overdue = df[(~df.paid) & df.due.notna() & (df.due < date.today())].shape[0]
    return int(paid_late + overdue)


def _seasonal_ref(s: pd.DataFrame, month) -> float | None:
    prev = s[s["month"] == month - 12]["amount"]
    return float(prev.iloc[0]) if len(prev) else None


def _prior_for(db: Session, user_id: int, category: str):
    prof = get_profile(db, user_id) if category == "electricity" else None
    return pm.make_prior(prof) if _has_answers(prof) else None


def _expected_high(prior, vals, ords, i):
    """Upper end of the calibrated model's 90% range for item i, using only data before it."""
    if prior is None or i < 1:
        return None
    return ml.ensemble_forecast(vals[:i], ords[:i], ords[i], prior)["high"]


def latest_anomalies(db: Session, user_id: int) -> dict[str, dict]:
    out = {}
    for cat in set(monthly(db, user_id)["category"]):
        s = category_series(db, user_id, cat, clean=True)
        if len(s) >= 4:
            vals, ords = s["amount"].tolist(), [p.ordinal for p in s["month"]]
            out[cat] = ml.detect_anomaly(vals[:-1], vals[-1], _seasonal_ref(s, s["month"].iloc[-1]),
                                         _expected_high(_prior_for(db, user_id, cat), vals, ords, len(vals) - 1))
    return out


def get_profile(db: Session, user_id: int) -> dict | None:
    p = db.query(HouseholdProfile).filter_by(user_id=user_id).first()
    return {f: getattr(p, f) for f in pm.PROFILE_FIELDS} if p else None


def _has_answers(p: dict | None) -> bool:
    return bool(p) and any(p.get(k) is not None for k in ("city", "climate_zone", "area_sqft", "occupants", "ac_count"))


def forecast_for(db: Session, user_id: int, category: str) -> dict:
    full = category_series(db, user_id, category)
    clean = category_series(db, user_id, category, clean=True)
    used = set(clean["month"])
    values, ords = clean["amount"].tolist(), [p.ordinal for p in clean["month"]]
    next_p = (clean["month"].iloc[-1] + 1) if len(clean) else pd.Period(date.today(), "M") + 1

    prof = get_profile(db, user_id) if category == "electricity" else None
    prior, comp, drv = None, None, None
    if _has_answers(prof):
        prior, comp = pm.make_prior(prof), pm.completeness(prof)
        drv = pm.drivers(prof, next_p.month)
    prior_err = 0.30 - 0.12 * (comp or 0) / 100  # more answers -> tighter cold-start range
    f = ml.ensemble_forecast(values, ords, next_p.ordinal, prior, prior_err)

    bt = [{"month": str(pd.Period(ordinal=r["ord"], freq="M")), "actual": r["actual"], "predicted": r["predicted"]} for r in (f or {}).get("backtest", [])]
    out = {
        "category": category,
        "history": [{"month": str(m), "amount": round(a, 2), "excluded": m not in used} for m, a in zip(full["month"], full["amount"])],
        "forecast": ({k: v for k, v in f.items() if k != "backtest"} | {"month": str(next_p)}) if f else None,
        "backtest": bt, "mape": f["mape"] if f else None,
        "profile_used": prior is not None, "profile_completeness": comp if prior is not None else pm.completeness(prof),
        "drivers": drv,
    }
    return out


def what_if(db: Session, user_id: int, changes: dict) -> dict:
    prof = get_profile(db, user_id)
    if not _has_answers(prof):
        raise ValueError("Fill in 'My home' first so we have a baseline.")
    base_prior = pm.make_prior(prof)
    new_prior = pm.make_prior({**prof, **{k: v for k, v in changes.items() if v is not None}})
    s = category_series(db, user_id, "electricity", clean=True)
    k = ml.calibration_factor(s["amount"].tolist(), [p.ordinal for p in s["month"]], base_prior) if len(s) else 1.0
    base = [base_prior.to_amount(base_prior.kwh(m) * k) for m in range(1, 13)]
    new = [new_prior.to_amount(new_prior.kwh(m) * k) for m in range(1, 13)]
    return {"baseline_annual": round(sum(base)), "new_annual": round(sum(new)), "annual_saving": round(sum(base) - sum(new)),
            "monthly_saving": round((sum(base) - sum(new)) / 12), "calibration": round(k, 2),
            "months": [{"month": m, "baseline": round(b), "new": round(n)} for m, b, n in zip(range(1, 13), base, new)]}


def anomaly_scan(db: Session, user_id: int) -> list[dict]:
    found = []
    for cat in sorted(set(monthly(db, user_id)["category"])):
        s = category_series(db, user_id, cat, clean=True)
        vals, months = s["amount"].tolist(), s["month"].tolist()
        lookup, ords, prior = dict(zip(months, vals)), [p.ordinal for p in months], _prior_for(db, user_id, cat)
        for i in range(3, len(vals)):
            r = ml.detect_anomaly(vals[:i], vals[i], lookup.get(months[i] - 12), _expected_high(prior, vals, ords, i))
            if r["is_anomaly"]:
                found.append({"category": cat, "month": str(months[i]), "amount": round(vals[i], 2), **r})
    return sorted(found, key=lambda r: r["month"], reverse=True)[:20]


def recommendations(db: Session, user_id: int) -> list[dict]:
    next_month = date.today().month % 12 + 1
    recs = recommend(all_series(db, user_id), late_count(db, user_id), latest_anomalies(db, user_id), next_month)
    prof = get_profile(db, user_id)
    if _has_answers(prof):  # personalised advice from what the user told us
        top = [d for d in pm.drivers(prof, 5) if d["kwh"] > 0][:1]
        if top and top[0]["name"] == "Air conditioning" and top[0]["share"] >= 40:
            recs.insert(0, {"category": "electricity", "priority": "high", "est_monthly_saving": None,
                            "title": f"Air conditioning is about {top[0]['share']}% of your summer electricity",
                            "detail": "Try the What-if tool on Insights: one less AC hour a day or a 1–2 °C higher setting often saves more than any other change."})
    return recs[:12]


def analyse_bill(db: Session, bill: Bill) -> dict:
    """Everything n8n needs to decide what to tell the user about one bill.

    Legacy rows can be missing an amount, period or due date. The analysis still
    returns a useful, honest answer instead of raising on ``float(None)``.
    """
    user = db.get(User, bill.user_id)
    s = category_series(db, bill.user_id, bill.category, clean=True)
    amount = float(bill.amount) if bill.amount is not None else None
    period_text = bill.period or (bill.due_date.strftime("%Y-%m") if bill.due_date else None)
    try:
        this_month = pd.Period(period_text, "M") if period_text else None
    except (TypeError, ValueError):
        this_month = None

    before = s[s["month"] < this_month] if this_month is not None else s.iloc[0:0]
    prior_vals = before["amount"].tolist()
    current_total = (s[s["month"] == this_month]["amount"].sum() if this_month is not None else 0.0)
    cur = float(current_total) if current_total else (amount if amount is not None else 0.0)

    if amount is None:
        anomaly = {"is_anomaly": False, "severity": "none", "z_score": 0.0, "pct_vs_avg": 0.0,
                   "pct_vs_prev": 0.0, "avg": None, "method": "missing-amount",
                   "reason": "This bill has no amount recorded, so it cannot be compared with earlier bills."}
        exp_high = None
    else:
        prior = _prior_for(db, bill.user_id, bill.category)
        ords = [p.ordinal for p in before["month"]]
        if this_month is not None:
            ords.append(this_month.ordinal)
            exp_high = _expected_high(prior, prior_vals + [cur], ords, len(prior_vals))
        else:
            exp_high = None
        anomaly = ml.detect_anomaly(prior_vals, cur,
                                    _seasonal_ref(s, this_month) if this_month is not None else None,
                                    exp_high)
    fc = forecast_for(db, bill.user_id, bill.category)["forecast"]

    unit_rate = None
    if bill.units and amount is not None:
        prev = [float(b.amount) / b.units for b in db.query(Bill).filter(
            Bill.user_id == bill.user_id, Bill.category == bill.category, Bill.reviewed.is_(True),
            Bill.id != bill.id, Bill.units > 0, Bill.amount.isnot(None))]
        rate = amount / bill.units
        unit_rate = {"current": round(rate, 2), "avg_previous": round(sum(prev) / len(prev), 2) if prev else None}

    days_to_due = (bill.due_date - date.today()).days if bill.due_date else None
    recs = [r for r in recommendations(db, bill.user_id) if r["category"] in (bill.category, "all")][:3]
    return {
        "bill": {"id": bill.id, "user_id": bill.user_id, "category": bill.category, "provider": bill.provider,
                 "amount": amount, "due_date": bill.due_date.isoformat() if bill.due_date else None,
                 "days_to_due": days_to_due, "state": bill.state, "units": bill.units},
        "user": {"id": user.id, "name": user.name, "email": user.email, "notify_email": user.notify_email},
        "anomaly": anomaly, "forecast": fc, "unit_rate": unit_rate, "recommendations": recs,
    }


def persist_result(db: Session, bill: Bill, result: dict, create_alert: bool = True) -> None:
    # A confirmed bill can be deleted while its background analysis is still
    # running. Never write an analysis or alert back onto the removed bill.
    if db.query(Bill.id).filter(Bill.id == bill.id).first() is None:
        log.info("bill %s was deleted before its analysis was saved; result discarded", bill.id)
        return
    bill.analysis = result
    try:
        db.commit()
    except IntegrityError:
        # The bill disappeared between the check above and the commit (or the
        # user was deleted). Drop the late result instead of logging an error
        # the user can do nothing about.
        db.rollback()
        log.info("bill %s disappeared while its analysis was being saved; result discarded", bill.id)
        return

    if not create_alert or not result.get("is_anomaly"):
        return
    severity = result.get("severity")
    created = alerting.add_alert(
        db, user_id=bill.user_id, bill_id=bill.id, kind="anomaly",
        severity="critical" if severity in ("high", "critical") else "warning",
        message=(result.get("summary") or "Unusual bill detected")[:500],
    )
    if created:
        user = db.get(User, bill.user_id)
        if user and user.notify_email:
            from . import emailer  # local import keeps the analytics path independent of SMTP
            emailer.send_anomaly_alert(user, bill, result)


def local_verdict(a: dict) -> dict:
    """Python mirror of the n8n 'Build Verdict' node — used as the offline fallback."""
    an, b, f = a["anomaly"], a["bill"], a["forecast"]
    amount = b.get("amount")
    shown = f"₹{float(amount):,.0f}" if amount is not None else "amount not recorded"
    summary = (f"{b['category'].title()} bill of {shown}: {an['reason']}" if an["is_anomaly"]
               else f"{b['category'].title()} bill of {shown} looks normal.")
    tip = a["recommendations"][0]["title"] if a["recommendations"] else None
    return {"is_anomaly": an["is_anomaly"], "severity": an["severity"], "summary": summary,
            "forecast_next": f["amount"] if f else None, "top_tip": tip, "source": "local-fallback"}

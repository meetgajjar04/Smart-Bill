"""Smart Bill AI endpoints (Ollama): status, bill analysis, portfolio summary and chat."""
import ast
import calendar
import json
import logging
import re
from datetime import date, timedelta
from statistics import median

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import case
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Bill, User
from ..schemas import ChatIn
from ..security import rate_limit
from ..services import ai_cache, analysis, llm

router = APIRouter(tags=["ai"])
log = logging.getLogger("smartbill.ai")


def _owned(db: Session, user: User, bill_id: int) -> Bill:
    bill = db.get(Bill, bill_id)
    if not bill or bill.user_id != user.id:  # 404 so IDs can't be probed
        raise HTTPException(404, "Bill not found.")
    return bill


def _response(row, cached: bool, source: str | None = None) -> dict:
    """Serialize a cached insight with explicit provenance (never guess model output)."""
    payload = row.payload if row else None
    if source is None and payload is not None:
        # Old rows may only carry the provenance in the model column.
        source = payload.get("source") or (
            row.model if row.model in ("local-fallback", "unverified-fallback") else "model")
    return {
        "analysis": payload,
        "engine": "Smart Bill AI",
        "created_at": row.created_at.isoformat() if row else None,
        "cached": cached,
        "source": source,
        "fallback": bool(source and source != "model"),
    }


def _fail(e: llm.LLMError) -> HTTPException:
    if isinstance(e, llm.LLMBadResponse):
        return HTTPException(502, f"Smart Bill AI returned an unusable answer: {e}")
    return HTTPException(503, str(e))


@router.get("/ai/status")
def ai_status(_: User = Depends(get_current_user)):
    """Whether Smart Bill AI is reachable and the configured model is installed."""
    return llm.status()


@router.get("/bills/{bill_id}/ai-analysis")
def get_bill_analysis(bill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned(db, user, bill_id)
    row = ai_cache.latest(db, user.id, "bill", bill_id)
    return _response(row, cached=row is not None)


@router.post("/bills/{bill_id}/ai-analysis")
def run_bill_analysis(bill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Deep analysis of one bill with Smart Bill AI. Cached until the bill changes."""
    bill = _owned(db, user, bill_id)
    if not bill.reviewed:
        raise HTTPException(409, "Confirm this bill before analysing it.")
    if not bill.amount or not bill.period:
        raise HTTPException(409, "This bill needs an amount and a date before it can be analysed.")

    context = analysis.analyse_bill(db, bill)
    context["home_profile"] = analysis.get_profile(db, bill.user_id)
    context["bill_text"] = (bill.raw_text or "")[:5000]
    try:
        payload = llm.analyse_bill(context)
        if not _analysis_payload_safe(payload, context):
            log.warning("bill %s AI analysis quoted unverified figures; using the local summary", bill_id)
            payload = _local_bill_analysis(context)
    except llm.LLMError as e:
        log.warning("bill %s AI analysis failed: %s", bill_id, e)
        raise _fail(e)
    provenance = payload.get("source")
    if provenance:
        payload["fallback"] = True
    row = ai_cache.save(db, user.id, "bill", payload, bill_id=bill.id, model=provenance)
    return _response(row, cached=False, source=provenance or "model")


def _bill_row(b: Bill) -> dict:
    return {
        "category": b.category, "provider": b.provider,
        "month": str(b.period) if b.period else None,
        "amount": float(b.amount), "units": b.units,
        "due_date": b.due_date.isoformat() if b.due_date else None, "paid": b.paid,
    }


def _portfolio_context(db: Session, user: User) -> dict:
    """Small, fast snapshot. Forecasts/anomaly scanning already have their own UI cards;
    running them here made the AI button wait tens of seconds before the model started."""
    df = analysis.monthly(db, user.id)
    if df.empty:
        raise HTTPException(409, "Add and confirm a few bills first so there is something to analyse.")

    by_month = [{"month": str(month), "total": round(float(df[df["month"] == month]["amount"].sum()), 2)}
                for month in sorted(df["month"].unique())[-8:]]
    profile = analysis.get_profile(db, user.id) or {}
    recent = (db.query(Bill)
              .filter(Bill.user_id == user.id, Bill.reviewed.is_(True), Bill.amount.isnot(None))
              .order_by(case((Bill.bill_date.is_(None), 1), else_=0), Bill.bill_date.desc(), Bill.id.desc()).limit(8).all())
    today = date.today()
    upcoming = (db.query(Bill)
                .filter(Bill.user_id == user.id, Bill.reviewed.is_(True), Bill.paid.is_(False),
                        Bill.due_date.isnot(None))
                .order_by(Bill.due_date).limit(5).all())
    return {
        "currency": "INR",
        "today": today.isoformat(),
        "months_covered": int(df["month"].nunique()),
        "total_spend": round(float(df["amount"].sum()), 2),
        "by_month": by_month,
        "by_category": {c: round(float(df[df["category"] == c]["amount"].sum()), 2)
                        for c in sorted(df["category"].unique())},
        "home_profile": {k: v for k, v in profile.items() if v is not None},
        "recent_bills": [_bill_row(b) for b in recent],
        "upcoming_bills": [{
            "category": b.category, "provider": b.provider, "amount": float(b.amount),
            "due_date": b.due_date.isoformat(), "days_left": (b.due_date - today).days,
        } for b in upcoming],
    }


@router.get("/analytics/ai-summary")
def get_portfolio_analysis(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = ai_cache.latest(db, user.id, "portfolio")
    return _response(row, cached=row is not None)


@router.post("/analytics/ai-summary")
def run_portfolio_analysis(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Deep analysis across all bills with Smart Bill AI."""
    context = _portfolio_context(db, user)
    context["verified_facts"] = _portfolio_facts(context)
    try:
        payload = _sanitize_portfolio(llm.analyse_portfolio(context), context)
        if not (payload.get("headline") or payload.get("summary")):
            # Nothing safe survived. Ask once with the exact figures Python computed;
            # anything still unproven is dropped by the same sanitizer.
            log.warning("portfolio AI analysis had no verified sentences for user %s; asking for a rewrite", user.id)
            try:
                payload = _sanitize_portfolio(
                    llm.analyse_portfolio(context, correction=_portfolio_repair_hint(context)), context)
            except llm.LLMError as e:
                log.warning("portfolio repair pass failed for user %s: %s", user.id, e)
        model_written = bool(payload.get("headline") or payload.get("summary")
                             or payload.get("trends") or payload.get("risks"))
        payload = _fill_portfolio(payload, context)
        if model_written:
            payload.pop("source", None)
        else:
            log.warning("portfolio AI analysis was fully unverified for user %s; using the built-in summary", user.id)
            payload["source"] = "unverified-fallback"
    except (llm.LLMBadResponse, llm.LLMTimeout) as e:
        # A slow CPU can cut the model off. Return an instant built-in summary rather
        # than a red error, and mark it so the UI is transparent about it.
        log.warning("portfolio AI analysis fell back for user %s: %s", user.id, e)
        payload = llm.fallback_portfolio(context)
    except llm.LLMError as e:
        log.warning("portfolio AI analysis failed for user %s: %s", user.id, e)
        raise _fail(e)
    # The model never chooses a savings figure: recompute each estimate from the bills.
    actions = []
    for action in payload.get("actions") or []:
        if isinstance(action, dict):
            actions.append({**action,
                            "est_monthly_saving": _estimate_monthly_saving(context, action.get("title"))})
    payload["actions"] = actions
    provenance = payload.get("source")
    if provenance:
        payload["fallback"] = True
    row = ai_cache.save(db, user.id, "portfolio", payload, model=provenance)
    return _response(row, cached=False, source=provenance or "model")


def _month_label(period: str) -> str:
    return pd.Period(period, "M").to_timestamp().strftime("%B %Y")


_MONTH_NUMBERS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTH_NUMBERS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def _chat_context(db: Session, user: User) -> dict:
    """Snapshot with every total already calculated in Python.

    The deterministic answerer gets the full bill list. The model only receives
    ``_llm_context`` (a compact subset), so it can never do arithmetic on its own.
    """
    today = date.today()
    this_m, last_m = pd.Period(today, "M"), pd.Period(today, "M") - 1

    bills = (db.query(Bill)
             .filter(Bill.user_id == user.id, Bill.reviewed.is_(True), Bill.amount.isnot(None))
             .order_by(case((Bill.bill_date.is_(None), 1), else_=0), Bill.bill_date.desc(), Bill.id.desc()).all())

    def bill_dict(b: Bill) -> dict:
        return {
            "id": b.id,
            "provider": b.provider or b.category.title(),
            "category": b.category,
            "amount": float(b.amount),
            "bill_date": b.bill_date.isoformat() if b.bill_date else None,
            "due_date": b.due_date.isoformat() if b.due_date else None,
            "period": str(b.period)[:7] if b.period else None,
            "paid": bool(b.paid),
            "days_left": (b.due_date - today).days if b.due_date else None,
        }

    rows = [bill_dict(b) for b in bills]
    month_totals: dict[str, float] = {}
    category_totals: dict[str, float] = {}
    category_counts: dict[str, int] = {}
    for b in rows:
        if b["period"]:
            month_totals[b["period"]] = month_totals.get(b["period"], 0.0) + b["amount"]
        category_totals[b["category"]] = category_totals.get(b["category"], 0.0) + b["amount"]
        category_counts[b["category"]] = category_counts.get(b["category"], 0) + 1

    months = [{"month": m, "label": _month_label(m), "total": round(v, 2)}
              for m, v in sorted(month_totals.items())]
    categories = {
        c: {"total": round(category_totals[c], 2), "count": category_counts[c],
            "average_bill": round(category_totals[c] / category_counts[c], 2),
            "months": len({b["period"] for b in rows if b["category"] == c and b["period"]})}
        for c in sorted(category_totals)
    }

    unpaid_rows = [b for b in rows if not b["paid"]]
    paid_rows = [b for b in rows if b["paid"]]
    unpaid_total = round(sum(b["amount"] for b in unpaid_rows), 2)
    paid_total = round(sum(b["amount"] for b in paid_rows), 2)
    all_time = round(sum(b["amount"] for b in rows), 2)
    months_recorded = len(month_totals)
    ordered = sorted(unpaid_rows, key=lambda b: (b["due_date"] or "9999-12-31", -b["amount"]))

    def due_text(b: dict) -> str:
        if not b["due_date"]:
            return "no due date"
        days = (date.fromisoformat(b["due_date"]) - today).days
        return f"{b['due_date']} ({days} days away)"

    def bill_text(b: dict) -> str:
        return (f"{b['provider']} ({b['category']}) ₹{b['amount']:,.0f} "
                f"due {due_text(b)}")

    # Latest bill per category, compared with that category's normal month.
    latest_per_category = []
    for b in rows:
        if any(item["category"] == b["category"] for item in latest_per_category):
            continue
        series: dict[str, float] = {}
        for x in rows:
            if x["category"] == b["category"] and x["period"]:
                series[x["period"]] = series.get(x["period"], 0.0) + x["amount"]
        latest_month = b["period"]
        previous = [v for m, v in sorted(series.items()) if latest_month and m < latest_month][-6:]
        typical = round(median(previous), 2) if previous else None
        entry = {
            "category": b["category"],
            "provider": b["provider"],
            "amount": b["amount"],
            "bill_date": b["bill_date"],
            "due_date": b["due_date"],
            "paid": b["paid"],
            "typical_month": typical,
            "all_time_total": categories[b["category"]]["total"],
            "bill_count": categories[b["category"]]["count"],
            "average_bill": categories[b["category"]]["average_bill"],
        }
        if typical:
            entry["vs_typical_pct"] = round((b["amount"] - typical) / typical * 100, 1)
        latest_per_category.append(entry)
        if len(latest_per_category) >= 6:
            break

    current = round(month_totals.get(str(this_m), 0.0), 2)
    previous = round(month_totals.get(str(last_m), 0.0), 2)
    if previous:
        change = round((current - previous) / previous * 100, 1)
        change_text = f"{abs(change)}% {'more' if change > 0 else 'less'} than last month"
    else:
        change, change_text = None, "no bill last month to compare with"

    top_category = max(categories.items(), key=lambda kv: kv[1]["total"]) if categories else None
    top_provider = ""
    if top_category:
        top_provider = next((c["provider"] for c in latest_per_category
                             if c["category"] == top_category[0]), top_category[0].title())
    latest_overall = max(rows, key=lambda b: (b["bill_date"] or "", b["id"] or 0), default=None)
    top_month = max(months, key=lambda m: m["total"]) if months else None
    low_month = min(months, key=lambda m: m["total"]) if months else None
    quick_answers = {
        "total_unpaid_inr": unpaid_total,
        "unpaid_bill_count": len(unpaid_rows),
        "overdue_count": sum(1 for b in unpaid_rows if b["days_left"] is not None and b["days_left"] < 0),
        "pay_first": bill_text(ordered[0]) if ordered else "No unpaid bills.",
        "next_due": due_text(ordered[0]) if ordered else "No unpaid bills.",
        "next_due_bill": bill_text(ordered[0]) if ordered else "No unpaid bills.",
        "unpaid_bills_text": "; ".join(bill_text(b) for b in ordered[:10]) or "No unpaid bills.",
        "this_month": str(this_m),
        "this_month_spend_inr": current,
        "last_month": str(last_m),
        "last_month_spend_inr": previous,
        "change_vs_last_month": change_text,
        "change_vs_last_month_pct": change,
        "change_inr": round(current - previous, 2),
        "most_expensive_month": (f"{top_month['label']} ({top_month['month']}) "
                                 f"₹{top_month['total']:,.0f}" if top_month else "no bills yet"),
        "cheapest_month": (f"{low_month['label']} ({low_month['month']}) "
                           f"₹{low_month['total']:,.0f}" if low_month else "no bills yet"),
        "largest_category_all_time": (
            f"{top_provider} ({top_category[0]}) ₹{top_category[1]['total']:,.0f}"
            if top_category else "no bills yet"),
        "biggest_unpaid_bill": (
            bill_text(max(unpaid_rows, key=lambda b: b["amount"])) if unpaid_rows else "No unpaid bills."),
        "average_monthly_spend_inr": (
            round(all_time / months_recorded, 2) if months_recorded else 0.0),
        "all_time_spend_inr": all_time,
        "paid_total_inr": paid_total,
        "paid_bill_count": len(paid_rows),
        "months_recorded": months_recorded,
        "latest_bill": bill_text(latest_overall) if latest_overall else "No bills yet.",
    }

    profile = analysis.get_profile(db, user.id) or {}
    return {
        "currency": "INR",
        "today": today.isoformat(),
        "user_name": user.name,
        "quick_answers": quick_answers,
        "totals": {
            "all_time": all_time,
            "unpaid": unpaid_total,
            "paid": paid_total,
            "bill_count": len(rows),
            "unpaid_count": len(unpaid_rows),
            "paid_count": len(paid_rows),
            "months_recorded": months_recorded,
            "overdue_count": quick_answers["overdue_count"],
        },
        "months": months,
        "categories": categories,
        "by_category": {c: v["total"] for c, v in categories.items()},
        "bills": rows,
        "recent_bills": rows[:12],
        "latest_bill": latest_overall,
        "unpaid_bills": ordered,
        "latest_per_category": latest_per_category,
        "home": {k: v for k, v in profile.items() if v is not None},
    }


# Everything the model may see. It is a compact fact sheet, not the raw bill table,
# so the prompt stays small while still containing every figure the model can quote.
_LLM_CONTEXT_KEYS = ("currency", "today", "user_name", "quick_answers", "totals",
                     "months", "categories", "latest_per_category", "latest_bill",
                     "recent_bills", "home")


def _llm_context(context: dict) -> dict:
    out = {k: context[k] for k in _LLM_CONTEXT_KEYS if k in context}
    out["months"] = list((context.get("months") or [])[-18:])
    out["unpaid_bills"] = list((context.get("unpaid_bills") or [])[:12])

    def size() -> int:
        return len(json.dumps(out, default=str, ensure_ascii=False, separators=(",", ":")))

    # Keep the fact sheet valid JSON: trim the least important lists before the
    # serializer has to cut it mid-object.
    while size() > llm.MAX_CONTEXT_CHARS:
        if len(out.get("recent_bills") or []) > 4:
            out["recent_bills"] = out["recent_bills"][:-1]
        elif len(out.get("months") or []) > 6:
            out["months"] = out["months"][1:]  # drop the oldest month, keep recent ones
        elif len(out.get("unpaid_bills") or []) > 4:
            out["unpaid_bills"] = out["unpaid_bills"][:-1]
        elif len(out.get("latest_per_category") or []) > 4:
            out["latest_per_category"] = out["latest_per_category"][:-1]
        elif out.pop("home", None) is not None:
            continue
        else:
            break
    return out


def _money(value: float) -> str:
    return f"₹{float(value or 0):,.0f}"


_TIPS = {
    "electricity": "run the AC an hour less, switch to LED bulbs, unplug devices on standby and check the meter reading",
    "mobile": "check your data usage and move to a cheaper plan that fits it",
    "internet": "compare speed tiers and ask your provider for a retention plan",
    "broadband": "compare speed tiers and ask your provider for a retention plan",
    "water": "fix leaking taps and shorten showers",
    "gas": "check for leaks and use efficient burners",
}

_CATEGORY_SYNONYMS = {
    "electricity": ("electricity", "electric", "power", "energy"),
    "mobile": ("mobile", "phone", "cell", "sim"),
    "internet": ("internet", "broadband", "wifi", "fiber", "fibre"),
    "water": ("water",),
    "gas": ("gas", "lpg"),
}


def _month_names_in_q(q: str) -> list[str]:
    found = []
    for name in _MONTH_NUMBERS:
        if name == "may":
            if re.search(r"\bin may\b", q):
                found.append(name)
        elif re.search(rf"\b{re.escape(name)}\b", q):
            found.append(name)
    return found


def _bill_label(b: dict) -> str:
    provider = b.get("provider") or (b.get("category") or "").title()
    category = b.get("category")
    if category and provider.lower() != category.lower():
        return f"{provider} ({category})"
    return provider


def _bill_line(b: dict, include_status: bool = False) -> str:
    parts = [f"{_bill_label(b)} {_money(b.get('amount', 0))}"]
    if b.get("due_date"):
        parts.append(f"due {b['due_date']}")
    if include_status:
        parts.append("paid" if b.get("paid") else "unpaid")
    return " ".join(parts)


def _resolve_entity(q: str, context: dict) -> dict | None:
    """Find the provider or category the user is talking about.

    Provider tokens must be distinctive (not a category word), so "electricity"
    resolves to the category instead of accidentally matching "Adani Electricity".
    """
    bills = context.get("bills") or []
    categories = {b.get("category") for b in bills if b.get("category")}
    category_words = set()
    for cat in categories:
        category_words.add(cat.lower())
        category_words.update(_CATEGORY_SYNONYMS.get(cat, ()))
    providers = []
    for b in bills:
        p = b.get("provider")
        if p and p not in providers:
            providers.append(p)
    for provider in sorted(providers, key=len, reverse=True):
        tokens = [w for w in re.split(r"[^a-z0-9]+", provider.lower())
                  if len(w) >= 3 and w not in category_words]
        if re.search(rf"\b{re.escape(provider.lower())}\b", q) or any(
                re.search(rf"\b{re.escape(w)}\b", q) for w in tokens):
            category = next((b.get("category") for b in bills if b.get("provider") == provider), None)
            return {"provider": provider, "category": category}
    for category in sorted(categories, key=len, reverse=True):
        names = set(_CATEGORY_SYNONYMS.get(category, ())) | {category.lower()}
        if any(re.search(rf"\b{re.escape(n)}\b", q) for n in names):
            return {"provider": None, "category": category}
    return None


def _entity_label(entity: dict | None) -> str:
    if not entity:
        return "your bills"
    return entity.get("provider") or (entity.get("category") or "bills").title()


def _entity_rows(rows: list[dict], entity: dict | None) -> list[dict]:
    if not entity:
        return list(rows)
    out = rows
    if entity.get("provider"):
        out = [b for b in out if (b.get("provider") or "").lower() == entity["provider"].lower()]
    if entity.get("category"):
        out = [b for b in out if (b.get("category") or "").lower() == entity["category"].lower()]
    return out


def _month_scope(q: str, context: dict) -> dict:
    """Resolve the time range in a question: month, week, year or all time."""
    today = date.fromisoformat(context["today"])
    periods = [m["month"] for m in context.get("months") or []]
    if "last month" in q:
        p = str(pd.Period(today, "M") - 1)
        return {"kind": "month", "period": p, "label": _month_label(p)}
    if "this month" in q:
        p = str(pd.Period(today, "M"))
        return {"kind": "month", "period": p, "label": _month_label(p)}
    if "next month" in q:
        p = str(pd.Period(today, "M") + 1)
        return {"kind": "month", "period": p, "label": _month_label(p)}
    if "last week" in q:
        start = today - timedelta(days=today.weekday() + 7)
        return {"kind": "week", "start": start, "end": start + timedelta(days=6), "label": "last week"}
    if "this week" in q:
        start = today - timedelta(days=today.weekday())
        return {"kind": "week", "start": start, "end": start + timedelta(days=6), "label": "this week"}
    if "next week" in q:
        start = today + timedelta(days=7 - today.weekday())
        return {"kind": "week", "start": start, "end": start + timedelta(days=6), "label": "next week"}
    if "last year" in q:
        return {"kind": "year", "year": today.year - 1, "label": str(today.year - 1)}
    if "this year" in q:
        return {"kind": "year", "year": today.year, "label": str(today.year)}
    names = _month_names_in_q(q)
    if names:
        name = names[0]
        number = _MONTH_NUMBERS[name]
        year_match = re.search(r"\b(20\d{2})\b", q)
        year = int(year_match.group(1)) if year_match else None
        matches = [p for p in periods
                   if int(p[5:7]) == number and (year is None or int(p[:4]) == year)]
        if matches:
            p = sorted(matches)[-1]
            return {"kind": "month", "period": p, "label": _month_label(p)}
        label = f"{name.title()} {year}" if year else name.title()
        return {"kind": "missing_month", "label": label}
    return {"kind": "all", "label": "all time"}


def _scope_rows(rows: list[dict], scope: dict) -> list[dict]:
    kind = scope.get("kind")
    if kind == "month":
        return [b for b in rows if b.get("period") == scope.get("period")]
    if kind == "year":
        year = str(scope.get("year"))
        return [b for b in rows if (b.get("period") or "").startswith(year)
                or (b.get("bill_date") or "").startswith(year)]
    if kind == "week":
        start, end = scope["start"], scope["end"]
        out = []
        for b in rows:
            if not b.get("bill_date"):
                continue
            if start <= date.fromisoformat(b["bill_date"]) <= end:
                out.append(b)
        return out
    return list(rows)


def _latest_bill(rows: list[dict], entity: dict | None) -> dict | None:
    candidates = _entity_rows(rows, entity)
    return max(candidates, key=lambda b: (b.get("bill_date") or "", b.get("id") or 0), default=None)


def _category_item(context: dict, entity: dict | None) -> dict | None:
    if not entity:
        return None
    items = context.get("latest_per_category") or []
    for item in items:
        if entity.get("category") and item.get("category") == entity["category"]:
            if not entity.get("provider") or (item.get("provider") or "").lower() == entity["provider"].lower():
                return item
    for item in items:
        if entity.get("category") and item.get("category") == entity["category"]:
            return item
    return None


def _format_number(value) -> str:
    """Format a calculated value with Indian-style grouping and no float noise."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


_MATH_FILLER = (
    "what", "what's", "whats", "is", "are", "the", "value", "of", "answer", "calculate", "compute",
    "solve", "evaluate", "please", "can", "you", "tell", "me", "how", "much", "equals", "equal",
    "result", "it", "that", "this", "rupees", "rupee", "rs", "inr", "₹", "total",
)


def _small_number(node) -> bool:
    """A literal number small enough that maths on it cannot build a huge integer."""
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return False
    if isinstance(node.value, int):
        return abs(node.value) <= 10 ** 15
    return isinstance(node.value, float) and abs(node.value) <= 1e15


def _calculate(expression: str):
    """Evaluate a pure arithmetic expression safely. Returns None if it is not arithmetic.

    Only numbers, operators and parentheses are allowed. Powers are restricted to
    two small literals: ``9**9**9`` and ``2**(10**100)`` would otherwise build a
    multi-megabyte integer inside ``eval`` and hang or exhaust the server.
    """
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except (SyntaxError, MemoryError):
        return None
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult,
               ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Load)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return None
        if isinstance(node, ast.Constant) and not _small_number(node):
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not _small_number(node.left) or not _small_number(node.right):
                return None
            if abs(node.right.value) > 100:
                return None
    try:
        return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})
    except ZeroDivisionError:
        return "zero-division"
    except (OverflowError, ValueError, TypeError, MemoryError):
        return None


def _math_reply(question: str) -> str | None:
    """Answer a plain arithmetic or percentage question exactly, without the model.

    "what is 7894*5879" -> 46,408,826. Anything that is not pure maths returns None
    so the bill engine (or the model) can handle it.
    """
    text = (question or "").lower().replace("×", "*").replace("÷", "/").replace("−", "-")
    text = re.sub(r"\bmultiplied\s+by\b", "*", text)
    text = re.sub(r"\bdivided\s+by\b", "/", text)
    text = re.sub(r"\b(?:to\s+the\s+power\s+of|raised\s+to)\b", "^", text)
    text = re.sub(r"\btimes\b", "*", text)
    text = re.sub(r"\bplus\b", "+", text)
    text = re.sub(r"\bminus\b", "-", text)
    text = re.sub(r"\bmod(?:ulo)?\b", "%", text)
    text = re.sub(r"(\d)\s*x\s*(\d)", r"\1*\2", text)

    percent = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s+cent\b)\s*of\s*([\d,]+(?:\.\d+)?)", text)
    if percent:
        pct, base = float(percent.group(1)), float(percent.group(2).replace(",", ""))
        return f"{_format_number(pct)}% of {_format_number(base)} is {_format_number(pct / 100 * base)}."

    natural = [
        (r"\b(?:difference|diff)\s+(?:between\s+)?([\d,.]+)\s+and\s+([\d,.]+)", "-"),
        (r"\bsum\s+of\s+([\d,.]+)\s+and\s+([\d,.]+)", "+"),
        (r"\bproduct\s+of\s+([\d,.]+)\s+and\s+([\d,.]+)", "*"),
    ]
    for pattern, operator in natural:
        found = re.search(pattern, text)
        if found:
            left = found.group(1).replace(",", "")
            right = found.group(2).replace(",", "")
            value = _calculate(f"{left}{operator}{right}")
            if isinstance(value, (int, float)):
                return f"{_format_number(float(left))} {operator} {_format_number(float(right))} = {_format_number(value)}"
            return None

    for word in _MATH_FILLER:
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text)
    text = text.replace("?", " ").replace("=", " ").strip()
    if not re.fullmatch(r"[0-9+\-*/%^().,\s]+", text):
        return None
    expression = text.replace(",", "").replace(" ", "")
    if not re.search(r"\d", expression) or not re.search(r"[+\-*/%^]", expression):
        return None
    value = _calculate(expression)
    if value is None:
        return None
    if value == "zero-division":
        return "That division is undefined because you cannot divide by zero."
    return f"{expression.replace('**', '^')} = {_format_number(value)}"


def _quick_reply(question: str, context: dict) -> str | None:
    """Offline exact-answer reference, no longer used by the chat endpoints.

    Chat replies are written by the model and checked with ``_answer_numbers_safe``.
    This deterministic engine is kept as a test oracle and for offline audits.
    """
    q = " " + re.sub(r"\s+", " ", (question or "").lower()).strip() + " "
    today = date.fromisoformat(context["today"])
    quick = context.get("quick_answers") or {}
    bills = context.get("bills") or []
    unpaid = context.get("unpaid_bills") or []
    cat_map = context.get("categories") or {}
    latest = context.get("latest_per_category") or []
    months = context.get("months") or []
    totals = context.get("totals") or {}
    entity = _resolve_entity(q, context)
    scope = _month_scope(q, context)
    all_time = float(totals.get("all_time") or quick.get("all_time_spend_inr") or 0)

    def total(rows):
        return sum(float(b.get("amount") or 0) for b in rows)

    # 1. Saving advice
    if re.search(r"\b(reduce|lower|cut|save|saving|savings|tips?|advice|improve|switch|cheaper|negotiate)\b", q):
        item = _category_item(context, entity)
        if item:
            text = f"Your latest {_bill_label(item)} bill is {_money(item['amount'])}"
            typical, change = item.get("typical_month"), item.get("vs_typical_pct")
            if typical and change is not None:
                text += (f", which is {abs(change):,.0f}% {'above' if change >= 0 else 'below'} "
                         f"your typical {item['category']} month of {_money(typical)}")
            text += "."
            tip = _TIPS.get(item["category"])
            if tip:
                text += f" To bring it down: {tip}."
            data = cat_map.get(item["category"])
            if data:
                text += (f" You have spent {_money(data['total'])} on {item['category']} "
                         f"across {data['count']} bills.")
            return text
        top = max(cat_map.items(), key=lambda kv: kv[1]["total"], default=None)
        if not top:
            return "I do not have any confirmed bills yet. Add a bill and I can suggest where to save."
        text = (f"Your biggest cost is {top[0].title()} at {_money(top[1]['total'])} all time. "
                f"Focus on {top[0].title()} first - it has the most room to save.")
        if unpaid:
            text += f" Pay {_bill_line(unpaid[0])} first."
        return text

    # 2. Which bill to pay first
    if re.search(r"\b(pay|paid)\b", q) and re.search(r"\b(first|priority|prioritise|prioritize|next)\b", q):
        if not unpaid:
            return "You have no unpaid bills, so nothing is waiting to be paid."
        first = unpaid[0]
        others = [b for b in unpaid[1:] if b.get("due_date") == first.get("due_date")]
        text = f"Pay {_bill_line(first)} first - it is the biggest bill due on the nearest date."
        if others:
            text += (" The other bills due that day are "
                     + ", ".join(f"{b['provider']} {_money(b['amount'])}" for b in others) + ".")
        return text

    # 3. Bills due in a window
    window_re = re.compile(r"\b(this month|next month|this week|next week|soon|today|tomorrow|upcoming|coming)\b")
    due_re = re.compile(r"\b(due|deadline|payable|upcoming)\b")
    month_names = _month_names_in_q(q)
    if (due_re.search(q) or "need to pay" in q) and (window_re.search(q) or month_names):
        due_rows = [b for b in unpaid if b.get("due_date")]
        if entity:
            due_rows = _entity_rows(due_rows, entity)
        window = None
        if "this month" in q or "next month" in q:
            p = str(pd.Period(today, "M") + (1 if "next month" in q else 0))
            due_rows = [b for b in due_rows if b["due_date"][:7] == p]
            window = _month_label(p)
        elif month_names:
            period = scope.get("period")
            if period:
                due_rows = [b for b in due_rows if b["due_date"][:7] == period]
                window = scope["label"]
            else:
                due_rows, window = [], scope.get("label") or "that month"
        elif "today" in q:
            due_rows = [b for b in due_rows if b["due_date"] == today.isoformat()]
            window = "today"
        elif "tomorrow" in q:
            d = (today + timedelta(days=1)).isoformat()
            due_rows = [b for b in due_rows if b["due_date"] == d]
            window = "tomorrow"
        else:
            days = 14 if re.search(r"\b(soon|upcoming|coming)\b", q) else 7
            due_rows = [b for b in due_rows
                        if 0 <= (date.fromisoformat(b["due_date"]) - today).days <= days]
            window = f"in the next {days} days"
        if not due_rows:
            upcoming = _entity_rows(unpaid, entity) if entity else unpaid
            if upcoming:
                nxt = upcoming[0]
                lead = (f"No {_entity_label(entity)} bills are due {window}" if entity
                        else f"No unpaid bills are due {window}")
                return (f"{lead}. The next one is {_bill_line(nxt)} "
                        f"({nxt.get('days_left')} days away).")
            if entity:
                return f"No {_entity_label(entity)} bills are due {window}."
            return f"No unpaid bills are due {window} - you have no unpaid bills at all."
        return (f"{len(due_rows)} bill(s) due {window}, totalling {_money(total(due_rows))}: "
                + "; ".join(_bill_line(b) for b in due_rows) + ".")

    # 4. Overdue
    if re.search(r"\b(overdue|late|missed)\b", q):
        late = [b for b in unpaid if b.get("days_left") is not None and b["days_left"] < 0]
        if not late:
            return "Nothing is overdue right now - every unpaid bill still has time before its due date."
        return (f"{len(late)} overdue bill(s) totalling {_money(total(late))}: "
                + "; ".join(_bill_line(b) for b in late) + ".")

    # 5. Due date / when
    if re.search(r"\b(when|what date|which date|due date|deadline|how soon|last date|payment date|by when|due next|due first)\b", q):
        if entity:
            candidates = [b for b in _entity_rows(bills, entity) if b.get("due_date")]
            target = ([b for b in candidates if not b.get("paid")] or candidates)
            target = max(target, key=lambda b: (b.get("bill_date") or "", b.get("id") or 0), default=None)
            if target:
                days = target.get("days_left")
                if days is None:
                    days = (date.fromisoformat(target["due_date"]) - today).days
                when = "today" if days == 0 else f"in {days} days" if days > 0 else f"{abs(days)} days ago"
                state = "already paid" if target.get("paid") else "unpaid"
                return f"Your {_bill_label(target)} bill is due {target['due_date']} ({when}); it is {state}."
            return f"I do not have a due date for {_entity_label(entity)} yet."
        if not unpaid:
            return "You have no unpaid bills, so there is no upcoming due date."
        b = unpaid[0]
        return f"Your next bill is {_bill_line(b)} ({b.get('days_left')} days away)."

    # 6. List / show
    if re.search(r"\b(list|show|display|see|what are|give me)\b", q) and re.search(r"\b(bills?|unpaid|paid|dues?)\b", q):
        if "unpaid" in q or "due" in q or "owe" in q:
            rows, title = unpaid, "unpaid"
        elif "paid" in q:
            rows, title = [b for b in bills if b.get("paid")], "paid"
        else:
            rows, title = bills, "recorded"
        if entity:
            rows = _entity_rows(rows, entity)
            title = f"{title} {_entity_label(entity)}"
        if not rows:
            return f"You have no {title} bills."
        shown = rows[:12]
        extra = f" Showing the 12 most recent of {len(rows)}." if len(rows) > 12 else ""
        return (f"You have {len(rows)} {title} bill(s) totalling {_money(total(rows))}: "
                + "; ".join(_bill_line(b, include_status=True) for b in shown) + f".{extra}")

    # 7. Anomaly
    if re.search(r"\b(unusual\w*|anomal\w*|spike|too high|so high|higher than usual|than normal|not normal|abnormal|out of line|overpriced)\b", q):
        target = _category_item(context, entity)
        if target is None:
            scored = [c for c in latest if c.get("vs_typical_pct") is not None]
            target = max(scored, key=lambda c: abs(c["vs_typical_pct"]), default=None)
        if target and target.get("typical_month") and target.get("vs_typical_pct") is not None:
            change = target["vs_typical_pct"]
            direction = "above" if change >= 0 else "below"
            text = (f"Your {_bill_label(target)} bill of {_money(target['amount'])} is "
                    f"{abs(change):,.0f}% {direction} your typical {target['category']} month of "
                    f"{_money(target['typical_month'])}.")
            text += (" That is a real spike worth checking." if change >= 50
                     else " That is in line with your normal usage.")
            return text
        if target:
            return (f"Your latest {_bill_label(target)} bill is {_money(target['amount'])}. "
                    "I need at least two months of that bill before I can call it unusual.")
        return "I need at least two months of bills in a category before I can flag something as unusual."

    # 8. Percentage / share
    if re.search(r"\b(percent\w*|share|portion|fraction|proportion)\b", q):
        if not all_time:
            return "I do not have any confirmed bills yet, so there is no spending to compare."
        if entity:
            part = total(_entity_rows(bills, entity))
            label = _entity_label(entity)
        else:
            top = max(cat_map.items(), key=lambda kv: kv[1]["total"], default=None)
            if not top:
                return "I do not have any confirmed bills yet."
            part, label = top[1]["total"], top[0].title()
        return (f"{label} is {_money(part)} of your {_money(all_time)} all-time spending, "
                f"which is {part / all_time * 100:.1f}%.")

    # 9. Largest / smallest category or provider
    if re.search(r"\b(most|highest|biggest|largest|top|least|lowest|cheapest|smallest)\b", q) and re.search(
            r"\b(provider|company|category|categories|expense|spend on|cost)\b", q) and "bill" not in q and "month" not in q:
        lowest = bool(re.search(r"\b(least|lowest|cheapest|smallest)\b", q))
        if re.search(r"\b(provider|company)\b", q):
            provider_totals = {}
            for b in bills:
                p = b.get("provider") or (b.get("category") or "").title()
                provider_totals[p] = provider_totals.get(p, 0) + b["amount"]
            if not provider_totals:
                return "I do not have any confirmed bills yet."
            pick = min if lowest else max
            name, amount = pick(provider_totals.items(), key=lambda kv: kv[1])
            pct = amount / all_time * 100 if all_time else 0
            word = "least" if lowest else "most"
            return (f"You pay {name} the {word}: {_money(amount)} all time, "
                    f"which is {pct:.1f}% of your spending.")
        if not cat_map:
            return "I do not have any confirmed bills yet."
        pick = min if lowest else max
        cat, data = pick(cat_map.items(), key=lambda kv: kv[1]["total"])
        pct = data["total"] / all_time * 100 if all_time else 0
        word = "smallest" if lowest else "largest"
        return (f"{cat.title()} is your {word} category: {_money(data['total'])} all time "
                f"across {data['count']} bills, which is {pct:.1f}% of your spending.")

    # 10. Most/least expensive month
    if re.search(r"\b(which|what)\b.*\bmonth\b", q) and re.search(
            r"\b(most|highest|biggest|largest|cheapest|least|lowest|smallest|spend|spent|cost)\b", q):
        source = months
        if entity:
            grouped = {}
            for b in _entity_rows(bills, entity):
                if b.get("period"):
                    grouped[b["period"]] = grouped.get(b["period"], 0) + b["amount"]
            source = [{"month": m, "label": _month_label(m), "total": v} for m, v in sorted(grouped.items())]
        if not source:
            return f"I do not have any {_entity_label(entity)} bills yet."
        if re.search(r"\b(cheapest|least|lowest|smallest)\b", q):
            m, word = min(source, key=lambda x: x["total"]), "least"
        else:
            m, word = max(source, key=lambda x: x["total"]), "most"
        count = sum(1 for b in _entity_rows(bills, entity) if b.get("period") == m["month"])
        return f"You spent the {word} in {m['label']}: {_money(m['total'])} across {count} bill(s)."

    # 11. Compare this month with last month
    if re.search(r"\b(more|less|higher|lower|increase|decrease|compare|compared|versus|vs)\b", q) and re.search(
            r"\b(last month|this month|month)\b", q):
        if entity:
            rows = _entity_rows(bills, entity)
            this_period = str(pd.Period(today, "M"))
            last_period = str(pd.Period(today, "M") - 1)
            current = total([b for b in rows if b.get("period") == this_period])
            previous = total([b for b in rows if b.get("period") == last_period])
            label = _entity_label(entity)
            if not previous:
                return (f"There are no {label} bills recorded for {_month_label(last_period)}, so there is "
                        f"nothing to compare. This month so far is {_money(current)}.")
            change = current - previous
            pct = change / previous * 100
            direction = "more" if change >= 0 else "less"
            return (f"Your {label} spend this month ({_month_label(this_period)}) is {_money(current)}, "
                    f"which is {_money(abs(change))} ({abs(pct):.1f}%) {direction} than "
                    f"{_month_label(last_period)} ({_money(previous)}).")
        current = float(quick.get("this_month_spend_inr") or 0)
        previous = float(quick.get("last_month_spend_inr") or 0)
        if not previous:
            return (f"There are no bills recorded for last month, so there is nothing to compare. "
                    f"This month so far is {_money(current)}.")
        change = float(quick.get("change_inr") or (current - previous))
        pct = quick.get("change_vs_last_month_pct")
        direction = "more" if change >= 0 else "less"
        pct_text = f" ({abs(pct):.1f}%)" if pct is not None else ""
        return (f"This month ({quick.get('this_month')}) is {_money(current)}, which is "
                f"{_money(abs(change))}{pct_text} {direction} than last month's {_money(previous)}.")

    # 12. Average / typical
    if re.search(r"\b(average|typical|normal|usually|on average|per month|each month|every month|monthly)\b", q) \
            and not re.search(r"\b(how many|number of)\b", q):
        # "What should my monthly budget be?" is advice, not a figure request,
        # so let the model phrase it instead of returning a bare average.
        if re.search(r"\b(budget|recommend\w*|suggest\w*|advice|plan)\b", q):
            return None
        if entity:
            cat = entity.get("category")
            data = cat_map.get(cat) if cat else None
            item = _category_item(context, entity)
            if not data or not item:
                return f"I do not have enough {_entity_label(entity)} bills to work out an average yet."
            parts = []
            if re.search(r"\b(typical|normal|usually)\b", q) and item.get("typical_month"):
                parts.append(f"your typical {cat} month is {_money(item['typical_month'])}")
            if re.search(r"\b(monthly|per month|each month|every month)\b", q) and data.get("months"):
                parts.append(f"your average monthly {cat} spend is "
                             f"{_money(data['total'] / data['months'])} across {data['months']} month(s)")
            parts.append(f"your average {cat} bill is {_money(data['average_bill'])} across {data['count']} bills")
            text = f"For {_entity_label(entity)}, " + " and ".join(parts) + "."
            if item.get("typical_month") and item.get("vs_typical_pct") is not None:
                text += (f" The latest bill of {_money(item['amount'])} is "
                         f"{abs(item['vs_typical_pct']):,.0f}% "
                         f"{'above' if item['vs_typical_pct'] >= 0 else 'below'} that.")
            return text
        months_recorded = int(totals.get("months_recorded") or quick.get("months_recorded") or 0)
        if not months_recorded:
            return "I do not have any confirmed bills yet."
        bill_count = max(1, int(totals.get("bill_count") or 1))
        return (f"Your average monthly spend is {_money(all_time / months_recorded)} across "
                f"{months_recorded} month(s) of bills. Your average bill is "
                f"{_money(all_time / bill_count)}.")

    # 13. Largest / smallest bill
    if re.search(r"\b(largest|biggest|highest|smallest|cheapest|lowest|most expensive|least expensive|costs? the (most|least))\b", q) and re.search(r"\bbills?\b", q):
        unpaid_only = bool(re.search(r"\b(unpaid|due|owe|owed|outstanding)\b", q))
        rows = unpaid if unpaid_only else bills
        if entity:
            rows = _entity_rows(rows, entity)
        if not rows:
            if unpaid_only:
                return "You have no unpaid bills right now."
            return f"I do not have any {_entity_label(entity)} bills yet." if entity else "I do not have any confirmed bills yet."
        if re.search(r"\b(smallest|cheapest|lowest|least expensive|costs? the least)\b", q):
            target, word = min(rows, key=lambda b: b["amount"]), "smallest"
        else:
            target, word = max(rows, key=lambda b: b["amount"]), "largest"
        return f"Your {word} {'unpaid ' if unpaid_only else ''}bill is {_bill_line(target, include_status=True)}."

    # 14. Counts
    if re.search(r"\bhow many\b", q) or re.search(r"\bnumber of\b", q):
        if entity:
            rows = _entity_rows(bills, entity)
            if re.search(r"\bpaid\b", q):
                rows = [b for b in rows if b.get("paid")]
            elif re.search(r"\b(unpaid|owe|owed|due|outstanding)\b", q):
                rows = [b for b in rows if not b.get("paid")]
            return f"You have {len(rows)} {_entity_label(entity)} bill(s) totalling {_money(total(rows))}."
        if re.search(r"\bmonths?\b", q):
            return (f"You have bills for {int(totals.get('months_recorded') or 0)} month(s), "
                    f"covering {int(totals.get('bill_count') or 0)} bills in total.")
        if re.search(r"\b(paid|pay)\b", q):
            return (f"You have paid {int(totals.get('paid_count') or 0)} bill(s) totalling "
                    f"{_money(totals.get('paid') or quick.get('paid_total_inr') or 0)}.")
        if re.search(r"\b(unpaid|owe|owed|due|outstanding)\b", q):
            return (f"You have {int(totals.get('unpaid_count') or 0)} unpaid bill(s) totalling "
                    f"{_money(totals.get('unpaid') or quick.get('total_unpaid_inr') or 0)}.")
        if scope.get("kind") in ("month", "week", "year"):
            rows = _scope_rows(bills, scope)
            return f"You have {len(rows)} bill(s) in {scope['label']} totalling {_money(total(rows))}."
        return (f"You have {int(totals.get('bill_count') or 0)} bill(s) in total: "
                f"{int(totals.get('unpaid_count') or 0)} unpaid and {int(totals.get('paid_count') or 0)} paid, "
                f"across {int(totals.get('months_recorded') or 0)} month(s).")

    # 15. How much is owed
    if re.search(r"\b(owe|owed|outstanding|unpaid|dues?|due|balance|remaining|need to pay|should i pay|have to pay|left to pay|still to pay)\b", q) and re.search(
            r"\b(how much|what|total|altogether|overall|all|anything|any|left|remaining|balance|still)\b", q):
        rows = _entity_rows(unpaid, entity) if entity else unpaid
        if not rows:
            return f"You have no unpaid {_entity_label(entity)} bills." if entity else "You have no unpaid bills right now."
        return (f"You owe {_money(total(rows))} across {len(rows)} unpaid bill(s): "
                + "; ".join(_bill_line(b) for b in rows) + ".")

    # 16. Total paid / total spend / total bills
    if not entity and scope.get("kind") == "all" and re.search(
            r"\b(total|overall|altogether|all[- ]time|everything|summary|how much)\b", q) and re.search(
            r"\b(bill|bills|spend|spent|paid|pay|amount|cost|owe|due|balance)\b", q):
        if re.search(r"\b(paid|pay)\b", q):
            return (f"You have paid {_money(totals.get('paid') or quick.get('paid_total_inr') or 0)} "
                    f"across {int(totals.get('paid_count') or 0)} bill(s). Your unpaid balance is "
                    f"{_money(totals.get('unpaid') or 0)} across {int(totals.get('unpaid_count') or 0)} bill(s).")
        if re.search(r"\b(spend|spent|cost)\b", q):
            return (f"Your all-time recorded spending is {_money(all_time)} across "
                    f"{int(totals.get('bill_count') or 0)} bills in {int(totals.get('months_recorded') or 0)} month(s).")
        return (f"Your recorded bills total {_money(all_time)} across {int(totals.get('bill_count') or 0)} bills: "
                f"{_money(totals.get('unpaid') or 0)} unpaid and {_money(totals.get('paid') or 0)} paid.")

    # 16b. Latest / last / most recent bill
    if (re.search(r"\b(latest|last|recent|newest|current)\b", q)
            and re.search(r"\b(bill|bills|payment|amount|charge|invoice)\b", q)
            and not re.search(r"\b(last|this|next)\s+(month|week|year)\b", q)):
        rows = _entity_rows(bills, entity) if entity else bills
        if re.search(r"\b(unpaid|due|owe|owed|outstanding)\b", q):
            rows = [b for b in rows if not b.get("paid")]
        elif re.search(r"\bpaid\b", q):
            rows = [b for b in rows if b.get("paid")]
        target = _latest_bill(rows, entity)
        if not target:
            return f"I do not have any {_entity_label(entity)} bills yet." if entity else "I do not have any confirmed bills yet."
        unpaid_only = bool(re.search(r"\b(unpaid|due|owe|owed|outstanding)\b", q))
        return (f"Your latest {'unpaid ' if unpaid_only else ''}bill is "
                f"{_bill_line(target, include_status=True)}.")

    # 17. Spend or bill amount for a provider/category
    if entity:
        rows = _entity_rows(bills, entity)
        label = _entity_label(entity)
        if not rows:
            return f"I do not have any {label} bills yet."
        scoped = _scope_rows(rows, scope)
        past_pay = bool(re.search(r"\b(paid|did i pay|have i paid|i have paid|i paid)\b", q))
        asks_bill = bool(re.search(r"\bbills?\b", q)) and not re.search(r"\b(spend|spent|paid|pay|cost)\b", q)
        if asks_bill and scope.get("kind") == "all":
            item = _latest_bill(rows, entity)
            text = f"Your latest {label} bill is {_bill_line(item, include_status=True)}."
            if len(rows) > 1:
                text += f" You have {len(rows)} {label} bills totalling {_money(total(rows))}."
            return text
        if not scoped:
            item = _latest_bill(rows, entity)
            when = scope.get("label") or "that period"
            if item:
                return (f"I do not have a {label} bill recorded for {when}. "
                        f"Your latest one is {_bill_line(item, include_status=True)}.")
            return f"I do not have a {label} bill recorded for {when}."
        amount = total(scoped)
        if past_pay:
            paid_rows = [b for b in scoped if b.get("paid")]
            unpaid_rows = [b for b in scoped if not b.get("paid")]
            return (f"You have paid {_money(total(paid_rows))} to {label} and "
                    f"{_money(total(unpaid_rows))} is still unpaid; the total billed is "
                    f"{_money(amount)} across {len(scoped)} bill(s).")
        period_text = f" in {scope['label']}" if scope.get("kind") in ("month", "year", "week") else " in total"
        shown = scoped[:6]
        extra = f" Showing {len(shown)} of {len(scoped)}." if len(scoped) > 6 else ""
        return (f"You spent {_money(amount)} on {label}{period_text} across {len(scoped)} bill(s): "
                + "; ".join(_bill_line(b, include_status=True) for b in shown) + f".{extra}")

    # 18. Spend in a month, week or year
    if scope.get("kind") in ("month", "week", "year"):
        rows = _scope_rows(bills, scope)
        label = scope["label"]
        if not rows:
            return f"I do not have any bills recorded for {label}."
        amount = total(rows)
        if re.search(r"\b(paid|pay)\b", q):
            paid_rows = [b for b in rows if b.get("paid")]
            unpaid_rows = [b for b in rows if not b.get("paid")]
            return (f"In {label} you paid {_money(total(paid_rows))} across {len(paid_rows)} bill(s); "
                    f"{_money(total(unpaid_rows))} is unpaid. The total billed is {_money(amount)}.")
        text = f"You spent {_money(amount)} in {label} across {len(rows)} bill(s)."
        if scope.get("kind") == "month" and scope.get("period") == quick.get("this_month"):
            text += f" That is {quick.get('change_vs_last_month')}."
        if len(rows) <= 6:
            text += " " + "; ".join(_bill_line(b, include_status=True) for b in rows) + "."
        else:
            biggest = max(rows, key=lambda b: b["amount"])
            text += f" The largest was {_bill_line(biggest, include_status=True)}."
        return text

    # 19. Generic spend fallback
    if re.search(r"\b(spend|spent|cost|pay|paid|bill|bills|amount|charge|how much)\b", q):
        return (f"Your all-time recorded spending is {_money(all_time)} across "
                f"{int(totals.get('bill_count') or 0)} bills in {int(totals.get('months_recorded') or 0)} month(s). "
                f"You owe {_money(totals.get('unpaid') or 0)} across "
                f"{int(totals.get('unpaid_count') or 0)} unpaid bill(s).")
    return None


def _safe_summary(context: dict) -> str:
    """A complete, exact answer used whenever the model cannot be trusted."""
    quick = context.get("quick_answers") or {}
    totals = context.get("totals") or {}
    categories = context.get("categories") or {}
    unpaid = context.get("unpaid_bills") or []
    all_time = float(totals.get("all_time") or quick.get("all_time_spend_inr") or 0)
    parts = [
        f"Your recorded bills total {_money(all_time)} across "
        f"{int(totals.get('bill_count') or 0)} bills in {int(totals.get('months_recorded') or 0)} month(s).",
        f"You owe {_money(totals.get('unpaid') or quick.get('total_unpaid_inr') or 0)} across "
        f"{int(totals.get('unpaid_count') or 0)} unpaid bill(s).",
    ]
    if unpaid:
        parts.append(f"Next due: {_bill_line(unpaid[0])}.")
    parts.append(f"This month so far is {_money(quick.get('this_month_spend_inr') or 0)}, "
                 f"{quick.get('change_vs_last_month') or 'with no last-month comparison'}.")
    if categories:
        top = sorted(categories.items(), key=lambda kv: kv[1]["total"], reverse=True)[:6]
        parts.append("By category: " + "; ".join(f"{c.title()} {_money(v['total'])}" for c, v in top) + ".")
    parts.append(f"Paid so far: {_money(totals.get('paid') or quick.get('paid_total_inr') or 0)} "
                 f"across {int(totals.get('paid_count') or 0)} bill(s).")
    return " ".join(parts)


_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*(\d[\d,]*(?:\.\d+)?)", re.I)
_DIRECTION_RE = re.compile(
    r"\b(?:up|ris(?:e|es|ing|en)|rose|increas(?:e|es|ed|ing)|higher|surge[ds]?|jump(?:ed|s)?|"
    r"grow(?:s|ing|n)?|grew|down|fell|fall(?:s|en|ing)?|decreas(?:e|es|ed|ing)|lower|"
    r"drop(?:s|ped|ping)?|declin(?:e|es|ed|ing)|reduc(?:e|ed|tion))\b", re.I)
_YEAR_CLAIM_RE = re.compile(r"\b(?:past|last|this|over the)\s+year\b|\bannual(?:ly)?\b", re.I)
_CAUSE_RE = re.compile(r"\b(?:due to|because|driven by|caused by|thanks to)\b", re.I)
_SUPERLATIVE_COST_RE = re.compile(
    r"\b(?:biggest|largest|highest|most expensive)\s+(?:cost|costs|spend|spending|expense|expenses|category)\b",
    re.I)
_REFERENCE_START_RE = re.compile(r"^(?:this|that|it|these|those|they|such)\b", re.I)
_GROUPED_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?![\d.])")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s+cent\b)", re.I)
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Anything left after money, percentages, dates and years have been scrubbed. Small
# values are counts/days in normal prose; large bare numbers must still be verified.
_BARE_RE = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)(?![\d.,])")


def _allowed_numbers(context: dict):
    """Every amount, percentage, date and year Python has actually calculated."""
    quick = context.get("quick_answers") or {}
    totals = context.get("totals") or {}
    categories = context.get("categories") or {}
    latest = context.get("latest_per_category") or []
    amounts, pcts, dates, years = set(), set(), set(), set()
    scalars = set()  # counts, day gaps and other bare integers the model may quote

    def amount(value):
        try:
            amounts.add(round(float(value), 2))
        except (TypeError, ValueError):
            pass

    def scalar(value):
        try:
            scalars.add(round(abs(float(value)), 2))
        except (TypeError, ValueError):
            pass

    def when(value):
        if not value:
            return
        text = str(value)
        dates.add(text)
        try:
            years.add(int(text[:4]))
        except ValueError:
            pass

    for bill in context.get("bills") or []:
        amount(bill.get("amount"))
        scalar(bill.get("amount"))
        scalar(bill.get("days_left"))
        scalar(bill.get("id"))
        when(bill.get("bill_date"))
        when(bill.get("due_date"))
        if bill.get("period"):
            when(f"{bill['period']}-01")
    for month in context.get("months") or []:
        amount(month.get("total"))
        scalar(month.get("total"))
        if month.get("month"):
            when(f"{month['month']}-01")
    for data in categories.values():
        amount(data.get("total"))
        amount(data.get("average_bill"))
        scalar(data.get("total"))
        scalar(data.get("count"))
        scalar(data.get("average_bill"))
        scalar(data.get("months"))
    for item in latest:
        for key in ("amount", "typical_month", "all_time_total", "average_bill", "bill_count"):
            amount(item.get(key))
            scalar(item.get(key))
        if item.get("vs_typical_pct") is not None:
            pcts.add(round(float(item["vs_typical_pct"]), 2))
        when(item.get("bill_date"))
        when(item.get("due_date"))
    for key in ("total_unpaid_inr", "this_month_spend_inr", "last_month_spend_inr",
                "change_inr", "average_monthly_spend_inr", "all_time_spend_inr", "paid_total_inr"):
        amount(quick.get(key))
        scalar(quick.get(key))
    for key in ("unpaid_bill_count", "paid_bill_count", "months_recorded", "overdue_count"):
        scalar(quick.get(key))
        scalar(totals.get(key))
    for key in ("all_time", "unpaid", "paid"):
        amount(totals.get(key))
        scalar(totals.get(key))
    scalar(totals.get("bill_count"))
    scalar(len(context.get("months") or []))
    scalar(len(context.get("unpaid_bills") or []))
    if quick.get("change_vs_last_month_pct") is not None:
        pcts.add(round(float(quick["change_vs_last_month_pct"]), 2))
    all_time = float(totals.get("all_time") or quick.get("all_time_spend_inr") or 0)
    if all_time:
        for data in categories.values():
            pcts.add(round(data["total"] / all_time * 100, 2))
        provider_totals = {}
        for bill in context.get("bills") or []:
            provider = bill.get("provider") or (bill.get("category") or "").title()
            provider_totals[provider] = provider_totals.get(provider, 0) + bill.get("amount", 0)
        for value in provider_totals.values():
            pcts.add(round(value / all_time * 100, 2))
    when(context.get("today"))
    scalars.update(amounts)
    scalars.update(years)
    return amounts, pcts, dates, years, scalars


def _answer_numbers_safe(answer: str, context: dict, extra_text: str = "") -> bool:
    """Reject any model answer containing a number Python did not calculate.

    ``extra_text`` may hold the user's question and an exact arithmetic result, so
    numbers the user typed (and the verified answer to them) are considered facts.
    """
    if not answer:
        return False
    try:
        amounts, pcts, dates, years, scalars = _allowed_numbers(context)
        if extra_text:
            for raw in _AMOUNT_RE.findall(extra_text):
                value = round(float(raw.replace(",", "")), 2)
                amounts.add(value)
                scalars.add(value)
            scrubbed_extra = _PCT_RE.sub(" ", _AMOUNT_RE.sub(" ", extra_text))
            for raw in _GROUPED_RE.findall(scrubbed_extra):
                value = round(float(raw.replace(",", "")), 2)
                amounts.add(value)
                scalars.add(value)
            for raw in _PCT_RE.findall(extra_text):
                pcts.add(round(float(raw), 2))
            for raw in _ISO_RE.findall(extra_text):
                dates.add(raw)
            for raw in _YEAR_RE.findall(extra_text):
                years.add(int(raw))
            # The question and the exact arithmetic answer may contain a bare number
            # (for example 2015 in "7,894 - 5,879 = 2,015"). Treat those as verified.
            bare_extra = _YEAR_RE.sub(" ", _ISO_RE.sub(" ", scrubbed_extra))
            for raw in _GROUPED_RE.findall(bare_extra):
                bare_extra = bare_extra.replace(raw, " ", 1)
            for raw in _BARE_RE.findall(bare_extra):
                value = round(float(raw), 2)
                scalars.add(value)
                if value >= 100:
                    amounts.add(value)

        def close(value, allowed, tolerance):
            return any(abs(value - candidate) <= tolerance for candidate in allowed)

        for raw in _AMOUNT_RE.findall(answer):
            if not close(float(raw.replace(",", "")), amounts, 1.0):
                return False
        scrubbed = _PCT_RE.sub(" ", _AMOUNT_RE.sub(" ", answer))
        for raw in _GROUPED_RE.findall(scrubbed):
            if not close(float(raw.replace(",", "")), amounts, 1.0):
                return False
        for raw in _PCT_RE.findall(answer):
            if not close(float(raw), pcts, 0.6):
                return False
        for raw in _ISO_RE.findall(answer):
            if raw not in dates:
                return False
        for raw in _YEAR_RE.findall(answer):
            if int(raw) not in years and not close(float(raw), scalars, 1.0):
                return False
        # Finally, catch large bare numbers (the model writing 50000 without ₹ or commas).
        bare = _YEAR_RE.sub(" ", _ISO_RE.sub(" ", scrubbed))
        for raw in _GROUPED_RE.findall(bare):
            bare = bare.replace(raw, " ", 1)
        for raw in _BARE_RE.findall(bare):
            value = float(raw)
            if value >= 100 and not close(value, scalars, 1.0):
                return False
        return True
    except (TypeError, ValueError, AttributeError):
        log.exception("numeric guard could not read an answer")
        return False


def _verified_extras(question: str) -> str:
    """Question text plus the exact arithmetic answer, if the question is maths."""
    math = _math_reply(question or "")
    return f"{question or ''} {math or ''}".strip()


def _repair_hint(context: dict) -> str:
    """A short list of verified figures the model may quote in its rewrite."""
    quick = context.get("quick_answers") or {}
    totals = context.get("totals") or {}
    facts = [
        f"all-time total ₹{float(totals.get('all_time') or 0):,.0f}",
        f"unpaid total ₹{float(totals.get('unpaid') or 0):,.0f}",
        f"paid total ₹{float(totals.get('paid') or 0):,.0f}",
        f"bill count {int(totals.get('bill_count') or 0)}",
    ]
    if quick.get("latest_bill"):
        facts.append(f"latest bill {quick['latest_bill']}")
    if quick.get("next_due_bill"):
        facts.append(f"next due {quick['next_due_bill']}")
    if quick.get("pay_first"):
        facts.append(f"pay first {quick['pay_first']}")
    if quick.get("biggest_unpaid_bill"):
        facts.append(f"biggest unpaid {quick['biggest_unpaid_bill']}")
    if quick.get("most_expensive_month"):
        facts.append(f"most expensive month {quick['most_expensive_month']}")
    if quick.get("this_month_spend_inr") is not None:
        facts.append(f"this month ₹{float(quick['this_month_spend_inr']):,.0f}")
    if quick.get("change_vs_last_month"):
        facts.append(f"month change {quick['change_vs_last_month']}")
    for category, data in sorted((context.get("categories") or {}).items(),
                                 key=lambda kv: kv[1].get("total") or 0, reverse=True)[:5]:
        facts.append(f"{category} ₹{float(data.get('total') or 0):,.0f} across "
                     f"{int(data.get('count') or 0)} bills")
    return "; ".join(facts)


def _strip_unsafe_sentences(answer: str, context: dict, extras: str) -> str:
    """Keep only sentences whose figures Python can verify."""
    pieces = re.split(r"(?<=[.!?])\s+|\n+", answer or "")
    kept = [piece.strip() for piece in pieces
            if piece.strip() and _answer_numbers_safe(piece.strip(), context, extras)]
    return " ".join(kept)


_PAY_FIRST_RE = re.compile(
    r"\b(pay|paying)\b.*\b(first|next|now)\b|\b(which|what)\b.*\bbill\b.*\bpay\b", re.I)

# A question that asks about recorded bills rather than hypothetical advice. Month
# questions with none of these words (for example "how much should I budget in
# August?") are left to the model.
_BILL_QUERY_RE = re.compile(
    r"\b(bills?|invoices?|billing|dues?|charges?|spend(?:ing)?|spent|paid|pay|owe|owed|"
    r"amounts?|totals?|costs?|list|show|give|details?|summary|breakdown|table|tabular)\b",
    re.I)
_TABLE_REQUEST_RE = re.compile(
    r"\b(table|tabular|columns?|grid|spreadsheet|rows? and columns?)\b", re.I)
_COMPARE_MONTHS_RE = re.compile(
    r"\b(compare|compared|comparison|versus|vs\.?|difference|higher than|lower than|"
    r"more than|less than)\b", re.I)
_TABLE_SEP_RE = re.compile(r"\|\s*:?-{3,}")
_FULL_MONTH_NAMES = tuple(name.lower() for name in calendar.month_name if name)

# "No data / no bills / nothing recorded" wording that contradicts a month we know
# has bills. "No unpaid bills" is a separate, valid statement and is not matched.
_MISSING_CLAIM_RE = re.compile(
    r"\bno\s+(?:\w+\s+){0,3}(?:data|records?|information|entries|details)\b"
    r"|\bno\s+(?:specific\s+)?bills?\b(?!\s+(?:are\s+)?(?:unpaid|overdue))"
    r"|\b(?:do(?:es)? not|don't|dont)\s+(?:have|find|show|see)\b[^.\n]{0,40}"
    r"\b(?:data|bills?|records?|information|entries)\b"
    r"|\bnothing\b[^.\n]{0,30}\b(?:recorded|found|available)\b",
    re.I)


def _cell(value) -> str:
    """A markdown table cell that cannot break the table layout."""
    text = str(value if value not in (None, "") else "-").replace("|", "/")
    return re.sub(r"\s+", " ", text).strip()


def _amount_mentioned(value, text: str) -> bool:
    """True if an amount appears in the text with or without Indian grouping."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return False
    return f"{number:,.0f}" in text or f"{number:.0f}" in text


def _month_bill_facts(question: str, context: dict) -> dict | None:
    """The exact bills for the month a question asks about.

    Month questions used to reach the model with only a monthly total in its fact
    sheet, so it sometimes claimed a month with real bills had no data. This returns
    the actual rows so Python can prove the answer and quote a table.
    """
    q = " " + re.sub(r"\s+", " ", (question or "").lower()).strip() + " "
    scope = _month_scope(q, context)
    kind = scope.get("kind")
    if kind not in ("month", "missing_month"):
        return None
    if _COMPARE_MONTHS_RE.search(q):
        return None  # comparisons cover several months; leave them to the model
    names = _month_names_in_q(q)
    if len(names) > 1:
        return None
    if not names and kind == "missing_month":
        return None
    if not _BILL_QUERY_RE.search(q):
        return None

    wants_table = bool(_TABLE_REQUEST_RE.search(q))
    wants_bills = bool(_BILL_QUERY_RE.search(q))
    if kind == "missing_month":
        return {"missing": True, "label": scope["label"], "period": None,
                "rows": [], "total": 0.0, "wants_table": wants_table,
                "wants_bills": wants_bills}

    rows = _scope_rows(context.get("bills") or [], scope)
    if not rows:
        return {"missing": True, "label": scope["label"], "period": scope.get("period"),
                "rows": [], "total": 0.0, "wants_table": wants_table,
                "wants_bills": wants_bills}
    rows = sorted(rows, key=lambda b: (b.get("bill_date") or "", b.get("id") or 0))
    return {"missing": False, "label": scope["label"], "period": scope.get("period"),
            "rows": rows, "total": round(sum(float(b.get("amount") or 0) for b in rows), 2),
            "wants_table": wants_table, "wants_bills": wants_bills}


def _month_intent_text(facts: dict) -> str:
    """A verified sentence, plus the exact markdown table when one was requested."""
    label = facts["label"]
    if facts["missing"]:
        return f"I do not have any bills recorded for {label}."
    rows, total = facts["rows"], facts["total"]
    count = len(rows)
    head = f"{label}: {count} bill{'s' if count != 1 else ''} totalling {_money(total)}."
    if facts["wants_table"]:
        lines = [
            "| Provider | Category | Amount | Due date | Status |",
            "| --- | --- | --- | --- | --- |",
        ]
        for bill in rows:
            lines.append(
                "| {provider} | {category} | {amount} | {due} | {status} |".format(
                    provider=_cell(bill.get("provider") or (bill.get("category") or "").title()),
                    category=_cell((bill.get("category") or "").title()),
                    amount=_money(bill.get("amount")),
                    due=_cell(bill.get("due_date")),
                    status="Paid" if bill.get("paid") else "Unpaid",
                )
            )
        lines.append(f"| **Total** | | **{_money(total)}** | | |")
        return head + "\n\n" + "\n".join(lines)
    shown = rows[:6]
    extra = f" Showing {len(shown)} of {len(rows)}." if len(rows) > 6 else ""
    return (head + " " + "; ".join(_bill_line(b, include_status=True) for b in shown)
            + "." + extra)


def _month_answer_mismatch(facts: dict, answer: str, low: str) -> bool:
    """True when a reply contradicts or misses the verified month bills."""
    if facts["missing"]:
        # A missing month must not be answered with an invented table.
        return bool(facts["wants_table"] and _TABLE_SEP_RE.search(answer))
    if _MISSING_CLAIM_RE.search(low):
        return True
    label_month = facts["label"].split()[0].lower()
    mentioned = [name for name in _FULL_MONTH_NAMES
                 if re.search(rf"\b{name.title()}\b", answer)]
    if mentioned and label_month not in mentioned:
        return True
    if facts["wants_table"] and not _TABLE_SEP_RE.search(answer):
        return True
    if facts["wants_table"] or facts.get("wants_bills"):
        for bill in facts["rows"]:
            provider = (bill.get("provider") or "").lower()
            if provider and provider not in low:
                return True
            if not _amount_mentioned(bill.get("amount"), answer):
                return True
        if not _amount_mentioned(facts["total"], answer):
            return True
    return False


def _intent_answer(question: str, context: dict) -> str:
    """An exact, precomputed answer for a question the model tends to misread.

    Choosing between two unpaid bills is easy to get wrong (soonest due vs largest),
    so Python states the correct bill up front and the model only has to phrase it.
    Month bill questions get the real rows (and a ready table) for the same reason.
    """
    if _PAY_FIRST_RE.search(question or ""):
        quick = context.get("quick_answers") or {}
        first = quick.get("pay_first") or ""
        if first and first != "No unpaid bills.":
            text = (f"Pay first: {first}. Reason: it has the earliest due date among the "
                    f"unpaid bills.")
            biggest = quick.get("biggest_unpaid_bill") or ""
            if biggest and biggest != first:
                text += f" The biggest unpaid bill is {biggest}, but it is due later."
            return text
    facts = _month_bill_facts(question, context)
    if facts:
        return _month_intent_text(facts)
    return ""


def _intent_mismatch(question: str, answer: str, context: dict) -> bool:
    """True when a reply contradicts the exact bill Python can prove."""
    text = answer or ""
    low = text.lower()
    if _PAY_FIRST_RE.search(question or ""):
        quick = context.get("quick_answers") or {}
        first = quick.get("pay_first") or ""
        if first and first != "No unpaid bills.":
            provider = first.split(" (")[0].strip().lower()
            if provider and provider not in low:
                return True
    facts = _month_bill_facts(question, context)
    if facts:
        return _month_answer_mismatch(facts, text, low)
    return False


def _model_reply(question: str, context: dict, history: list[dict]) -> str:
    """Let qwen3 write the answer, then verify every figure it quotes.

    The model is the only thing that writes prose. If it invents a number, it is
    asked once to rewrite using the verified fact sheet; anything still unverified
    is dropped so a wrong figure can never reach the screen.
    """
    llm_context = _llm_context(context)
    math = _math_reply(question)
    intent = _intent_answer(question, context)
    if intent:
        # A month asked for "in a table" is a pure data request. Python already has
        # every row, so return the verified table immediately: exact and instant.
        facts = _month_bill_facts(question, context)
        if facts and not facts["missing"] and facts["wants_table"]:
            log.info("answering a month table request from verified rows")
            return intent
    if math or intent:
        # Give the model the exact result so it only has to phrase it, never calculate.
        llm_context = dict(llm_context)
        if math:
            llm_context["math_answer"] = math
        if intent:
            llm_context["intent_answer"] = intent
    extras = _verified_extras(question)
    answer = llm.chat_answer(question, llm_context, history)
    if (_answer_numbers_safe(answer, context, extras)
            and not _intent_mismatch(question, answer, context)):
        return answer

    if not intent and not _looks_numeric(question):
        # Advice questions often add optional savings examples. Dropping the one unsafe
        # sentence is faster and just as safe as asking the model for a full rewrite.
        cleaned = _strip_unsafe_sentences(answer, context, extras)
        if len(cleaned) >= 12:
            log.info("advice answer had an unverified figure; kept the safe sentences")
            return cleaned

    log.warning("chat answer quoted unverified figures (%r); asking the model to rewrite",
                (answer or "")[:280])
    hint = (
        "Rewrite the answer using only these verified figures, and do not calculate "
        f"any number yourself: {_repair_hint(context)}."
    )
    if math:
        hint += f" The exact arithmetic answer is: {math}"
    if intent:
        hint += f" Answer this exactly: {intent}"
    try:
        answer = llm.chat_answer(question, llm_context, history, correction=hint)
    except llm.LLMError as e:
        log.warning("chat repair pass failed: %s", e)
        answer = ""
    if (answer and _answer_numbers_safe(answer, context, extras)
            and not _intent_mismatch(question, answer, context)):
        return answer
    if answer:
        log.warning("chat repair still unverified (%r); dropping unsafe sentences",
                    answer[:280])

    if intent:
        # The figures were unsafe, the model named the wrong bill, or it claimed real
        # bills were missing. The precomputed answer is what Python can prove, so use
        # it rather than a maybe.
        log.warning("chat answer still unverified; using the exact precomputed answer")
        return intent

    cleaned = _strip_unsafe_sentences(answer, context, extras)
    if len(cleaned) >= 12:
        return cleaned
    return ("I could not verify every figure in that answer, so I would rather not guess. "
            "Please ask again, or check the Bills page for the exact figures.")


_NUMERIC_RE = re.compile(
    r"\b(how much|how many|total|totals|owe|owed|outstanding|unpaid|paid|pay|due|dues|spend|spent|cost|costs|"
    r"bill|bills|amount|amounts|average|typical|percent|percentage|share|which month|largest|biggest|smallest|"
    r"cheapest|highest|lowest|more than|less than|compare|compared|when is|due date|number of|balance)\b|₹|\d",
    re.I,
)


def _looks_numeric(question: str) -> bool:
    """True when a question expects a figure, so it must never reach the model unchecked."""
    q = (question or "").lower()
    if re.search(r"\b(reduce|lower|cut|save|saving|savings|tips?|advice|improve|switch|cheaper|negotiate)\b", q):
        return False
    return bool(_NUMERIC_RE.search(q))


# ------------------------------------------------ guards for analysis cards
_PCT_KEY_HINTS = ("pct", "percent", "share", "ratio")
_DATE_KEY_HINTS = ("date", "today", "month", "period")


def _scan_numbers(obj, amounts: set, pcts: set, dates: set, key: str = "") -> None:
    """Collect every number, percentage and date present in a computed context."""
    if isinstance(obj, dict):
        for name, value in obj.items():
            _scan_numbers(value, amounts, pcts, dates, str(name))
        return
    if isinstance(obj, (list, tuple)):
        for value in obj:
            _scan_numbers(value, amounts, pcts, dates, key)
        return
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        value = round(float(obj), 2)
        if any(hint in key.lower() for hint in _PCT_KEY_HINTS):
            pcts.add(value)
        else:
            amounts.add(value)
        return
    if isinstance(obj, str):
        text = obj.strip()
        if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", text):
            dates.add(text)
        for raw in _AMOUNT_RE.findall(text):
            amounts.add(round(float(raw.replace(",", "")), 2))
        for raw in _GROUPED_RE.findall(text):
            amounts.add(round(float(raw.replace(",", "")), 2))


def _derived_percentages(context: dict) -> set:
    """Percentages Python can prove: category shares and month-on-month change."""
    out = set()
    try:
        total = float(context.get("total_spend") or 0)
    except (TypeError, ValueError):
        total = 0.0
    for value in (context.get("by_category") or {}).values():
        try:
            if total:
                out.add(round(float(value) / total * 100, 2))
        except (TypeError, ValueError):
            continue
    totals = []
    for month in context.get("by_month") or []:
        if not isinstance(month, dict):
            continue
        try:
            totals.append(float(month.get("total") or 0))
        except (TypeError, ValueError):
            totals.append(0.0)
    for previous, current in zip(totals, totals[1:]):
        if previous:
            out.add(round((current - previous) / previous * 100, 2))
    return out


def _text_numbers_safe(text: str, amounts: set, pcts: set, dates: set, years: set) -> bool:
    if not isinstance(text, str) or not text:
        return True

    def close(value, allowed, tolerance):
        return any(abs(value - candidate) <= tolerance for candidate in allowed)

    try:
        for raw in _AMOUNT_RE.findall(text):
            if not close(float(raw.replace(",", "")), amounts, 1.0):
                return False
        scrubbed = _PCT_RE.sub(" ", _AMOUNT_RE.sub(" ", text))
        for raw in _GROUPED_RE.findall(scrubbed):
            if not close(float(raw.replace(",", "")), amounts, 1.0):
                return False
        for raw in _PCT_RE.findall(text):
            if not close(float(raw), pcts, 0.6):
                return False
        for raw in _ISO_RE.findall(text):
            if raw not in dates:
                return False
        for raw in _YEAR_RE.findall(text):
            if int(raw) not in years and not close(float(raw), amounts, 1.0):
                return False
        bare = _YEAR_RE.sub(" ", _ISO_RE.sub(" ", scrubbed))
        for raw in _GROUPED_RE.findall(bare):
            bare = bare.replace(raw, " ", 1)
        for raw in _BARE_RE.findall(bare):
            value = float(raw)
            if value >= 100 and not close(value, amounts, 1.0):
                return False
        return True
    except (TypeError, ValueError, AttributeError):
        log.exception("analysis numeric guard could not read a sentence")
        return False


def _payload_texts(obj) -> list[str]:
    """Every human-readable string in an analysis payload."""
    if isinstance(obj, dict):
        out = []
        for name, value in obj.items():
            if name == "est_monthly_saving":
                continue
            out.extend(_payload_texts(value))
        return out
    if isinstance(obj, (list, tuple)):
        out = []
        for value in obj:
            out.extend(_payload_texts(value))
        return out
    return [obj] if isinstance(obj, str) else []


def _portfolio_facts(context: dict) -> list[str]:
    """Plain-language findings calculated in Python, so the model only phrases them.

    The small analysis model tends to describe a category total as an increase, so
    these sentences state exactly what each number means. ``analyse_portfolio``
    receives them as ``verified_facts`` and the router rejects a model answer that
    misreads a total as a month-on-month change.
    """
    facts = []
    months_covered = int(context.get("months_covered") or 0)
    total = float(context.get("total_spend") or 0)
    if total:
        facts.append(f"Total spend over {months_covered} months is ₹{total:,.0f}; "
                     f"this is a total across all bills, not a monthly or yearly figure.")
    categories = sorted(((str(c), float(a or 0)) for c, a in (context.get("by_category") or {}).items()),
                        key=lambda kv: kv[1], reverse=True)
    for cat, amount in categories[:3]:
        facts.append(f"{cat.title()} totals ₹{amount:,.0f} across all its bills.")
    by_month = [m for m in (context.get("by_month") or []) if isinstance(m, dict)]
    if len(by_month) >= 2:
        latest, previous = by_month[-1], by_month[-2]
        cur = float(latest.get("total") or 0)
        prev = float(previous.get("total") or 0)
        change = cur - prev
        facts.append(f"{latest.get('month')} totals ₹{cur:,.0f} across all categories, which is ₹{abs(change):,.0f} "
                     f"{'higher' if change >= 0 else 'lower'} than {previous.get('month')} "
                     f"(₹{prev:,.0f}).")
    if by_month:
        high = max(by_month, key=lambda m: float(m.get("total") or 0))
        low = min(by_month, key=lambda m: float(m.get("total") or 0))
        facts.append(f"The highest month in the months shown is {high.get('month')} at "
                     f"₹{float(high.get('total') or 0):,.0f}; the lowest is {low.get('month')} at "
                     f"₹{float(low.get('total') or 0):,.0f}.")
    for item in (context.get("upcoming_bills") or [])[:3]:
        if isinstance(item, dict):
            days = item.get("days_left")
            when = (f"in {days} days" if isinstance(days, int) and days >= 0
                    else f"{-days} days ago" if isinstance(days, int) else "soon")
            facts.append(f"{item.get('provider') or str(item.get('category') or 'bill').title()} "
                         f"₹{float(item.get('amount') or 0):,.0f} is due on {item.get('due_date')} "
                         f"({when}).")
    return facts


def _portfolio_deltas(context: dict) -> set:
    """Real month-on-month changes, signed and as magnitudes, for claim checks."""
    totals = [float(m.get("total") or 0) for m in (context.get("by_month") or [])
              if isinstance(m, dict)]
    deltas = set()
    for previous, current in zip(totals, totals[1:]):
        deltas.add(round(current - previous, 2))
        deltas.add(round(abs(current - previous), 2))
    return deltas


def _portfolio_category_amounts(context: dict) -> dict[str, set[float]]:
    """Currency figures Python can prove *for each category*.

    The numeric guard proves a figure exists somewhere in the data. This narrower
    set proves it belongs to the category a sentence names, so the model cannot
    credit electricity with the month's combined total.
    """
    out: dict[str, set[float]] = {}
    for cat, total in (context.get("by_category") or {}).items():
        try:
            out.setdefault(str(cat), set()).add(round(float(total), 2))
        except (TypeError, ValueError):
            continue
    for key in ("recent_bills", "upcoming_bills"):
        for bill in context.get(key) or []:
            if not isinstance(bill, dict):
                continue
            cat = str(bill.get("category") or "")
            try:
                amount = round(float(bill.get("amount") or 0), 2)
            except (TypeError, ValueError):
                continue
            if cat:
                out.setdefault(cat, set()).add(amount)
    return out


def _category_amount_unsafe(sentence: str, category_amounts: dict[str, set[float]]) -> bool:
    """True when a sentence pairs a category with a figure that is not its own.

    Example the guard must catch: "the highest electricity bill was ₹34,000 in
    December" when ₹34,000 is every bill that month, not the electricity bill.
    """
    values = [float(raw.replace(",", "")) for raw in _AMOUNT_RE.findall(sentence)]
    if not values:
        return False
    for cat, allowed in category_amounts.items():
        # "other" is an ordinary English word; skip it rather than risk
        # dropping a correct sentence such as "on the other hand …".
        if cat == "other" or not re.search(rf"\b{re.escape(cat)}\b", sentence, re.I):
            continue
        if any(not any(abs(value - candidate) <= 1 for candidate in allowed) for value in values):
            return True
    return False


def _top_category(context: dict) -> str | None:
    """The category with the largest total, or None when the data has no totals."""
    totals: dict[str, float] = {}
    for cat, amount in (context.get("by_category") or {}).items():
        try:
            totals[str(cat)] = float(amount or 0)
        except (TypeError, ValueError):
            continue
    return max(totals, key=totals.get) if totals else None


def _category_superlative_unsafe(sentence: str, category_amounts: dict[str, set[float]],
                                 top_category: str | None) -> bool:
    """Only the category with the largest total may be called the biggest cost."""
    if not top_category or not _SUPERLATIVE_COST_RE.search(sentence):
        return False
    for cat in category_amounts:
        if cat == "other" or cat == top_category:
            continue
        if re.search(rf"\b{re.escape(cat)}\b", sentence, re.I):
            return True
    return False


def _sentence_claim_unsafe(sentence: str, deltas: set, months: int, categories: list[str],
                           category_amounts: dict[str, set[float]] | None = None,
                           top_category: str | None = None) -> bool:
    """True when one sentence claims a change or a time span the data cannot prove."""
    if _CAUSE_RE.search(sentence):
        # The bill data has no causes, so any causal claim is invented.
        return True
    if category_amounts and _category_amount_unsafe(sentence, category_amounts):
        return True
    if category_amounts and _category_superlative_unsafe(sentence, category_amounts, top_category):
        return True
    if _DIRECTION_RE.search(sentence):
        # Category totals are all-time sums, so a category is never proven to be rising.
        if any(re.search(rf"\b{re.escape(cat)}\b", sentence, re.I) for cat in categories):
            return True
        for raw in _AMOUNT_RE.findall(sentence):
            value = float(raw.replace(",", ""))
            if not any(abs(value - delta) <= 1 for delta in deltas):
                return True
    if months and months != 12 and _YEAR_CLAIM_RE.search(sentence) and _AMOUNT_RE.search(sentence):
        return True
    return False


def _portfolio_claim_mismatch(payload, context: dict) -> bool:
    """True when a model sentence describes a total as an increase, or invents a year.

    The numeric guard already proves every figure exists somewhere in the data. This
    check protects the meaning: a change sentence may only quote a real
    month-on-month difference, and a category total is never called a change.
    """
    deltas = _portfolio_deltas(context)
    months = int(context.get("months_covered") or 0)
    categories = [str(cat) for cat in (context.get("by_category") or {})]
    for text in _payload_texts(payload):
        # Judge each sentence on its own: a payload string can hold a legitimate
        # change sentence followed by a separate total.
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if sentence.strip() and _sentence_claim_unsafe(
                    sentence, deltas, months, categories, _portfolio_category_amounts(context),
                    _top_category(context)):
                return True
    return False


def _portfolio_text_safe(text, context: dict, *, claim: bool = True) -> bool:
    """Numeric check for every analysis sentence, plus the meaning check for prose."""
    if not isinstance(text, str) or not text.strip():
        return False
    amounts, pcts, dates = set(), set(), set()
    _scan_numbers(context, amounts, pcts, dates)
    pcts |= _derived_percentages(context)
    years = set()
    for value in dates:
        try:
            years.add(int(value[:4]))
        except ValueError:
            continue
    if not _text_numbers_safe(text, amounts, pcts, dates, years):
        return False
    if not claim:
        return True
    return not _sentence_claim_unsafe(text, _portfolio_deltas(context),
                                      int(context.get("months_covered") or 0),
                                      [str(cat) for cat in (context.get("by_category") or {})],
                                      _portfolio_category_amounts(context),
                                      _top_category(context))


def _keep_safe_sentences(text, context: dict, *, claim: bool = True) -> str:
    """Keep only the sentences of one field that both guards accept.

    A sentence that opens with "This/It/They..." depends on the sentence before it,
    so it is dropped when that sentence was dropped.
    """
    if not isinstance(text, str):
        return ""
    kept, previous_kept = [], False
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        safe = _portfolio_text_safe(sentence, context, claim=claim)
        if safe and not previous_kept and _REFERENCE_START_RE.match(sentence):
            safe = False
        if safe:
            kept.append(sentence)
        previous_kept = safe
    return " ".join(kept)


def _sanitize_portfolio(payload, context: dict) -> dict:
    """Return only the model's sentences that Python can prove, field by field."""
    if not isinstance(payload, dict):
        return {"headline": "", "summary": "", "trends": [], "risks": [], "actions": []}
    actions = []
    for item in payload.get("actions") or payload.get("recommendations") or []:
        if isinstance(item, str):
            title, detail, saving = _keep_safe_sentences(item, context, claim=False), "", None
        elif isinstance(item, dict):
            title = _keep_safe_sentences(item.get("title") or item.get("action"), context, claim=False)
            detail = _keep_safe_sentences(
                item.get("detail") or item.get("why") or item.get("description"), context, claim=False)
            try:
                saving = float(item.get("est_monthly_saving") or item.get("saving") or 0) or None
            except (TypeError, ValueError):
                saving = None
        else:
            continue
        if title:
            actions.append({"title": title, "detail": detail, "est_monthly_saving": saving})
    return {
        "headline": _keep_safe_sentences(payload.get("headline") or payload.get("title"), context),
        "summary": _keep_safe_sentences(payload.get("summary"), context),
        "trends": [_keep_safe_sentences(item, context)
                   for item in (payload.get("trends") or payload.get("observations") or [])
                   if _keep_safe_sentences(item, context)],
        "risks": [_keep_safe_sentences(item, context)
                  for item in (payload.get("risks") or payload.get("warnings") or [])
                  if _keep_safe_sentences(item, context)],
        "actions": actions,
    }


def _fill_portfolio(payload: dict, context: dict) -> dict:
    """Fill any dropped field with the verified built-in summary, so the card is complete."""
    fallback = llm.fallback_portfolio(context)
    if not payload.get("headline"):
        payload["headline"] = fallback["headline"]
    if not payload.get("summary"):
        facts = context.get("verified_facts") or _portfolio_facts(context)
        payload["summary"] = " ".join(facts[:2])
    if not payload.get("trends"):
        payload["trends"] = fallback["trends"]
    if not payload.get("risks"):
        payload["risks"] = fallback["risks"]
    if not payload.get("actions"):
        payload["actions"] = fallback["actions"]
    return payload


def _portfolio_repair_hint(context: dict) -> str:
    """The exact facts the portfolio model may quote when it rewrites.

    Everything here comes from ``_portfolio_context``/``_portfolio_facts``, so the
    numeric guard accepts it. Percentages are deliberately excluded: the model is
    told to avoid them.
    """
    facts = context.get("verified_facts") or _portfolio_facts(context)
    return ("Use only these verified facts: " + " ".join(facts) +
            " Do not use any percentage, share or percent sign anywhere, and never describe "
            "a category total as an increase. Only the month-to-month difference may be "
            "called higher or lower.")


def _analysis_payload_safe(payload, context: dict) -> bool:
    """Reject model-written analysis that quotes a figure Python did not compute."""
    if not isinstance(payload, dict):
        return False
    amounts, pcts, dates = set(), set(), set()
    _scan_numbers(context, amounts, pcts, dates)
    pcts |= _derived_percentages(context)
    years = set()
    for value in dates:
        try:
            years.add(int(value[:4]))
        except ValueError:
            continue
    return all(_text_numbers_safe(text, amounts, pcts, dates, years)
               for text in _payload_texts(payload))


def _estimate_monthly_saving(context: dict, title: str) -> int | None:
    """A rule-based saving estimate (10% of one category's average month).

    The model is never allowed to choose this figure itself.
    """
    by_category = context.get("by_category") or {}
    months = max(1, int(context.get("months_covered") or 1))
    name = (title or "").lower()
    chosen = None
    for category in by_category:
        synonyms = _CATEGORY_SYNONYMS.get(str(category).lower(), ())
        if str(category).lower() in name or any(word in name for word in synonyms):
            chosen = category
            break
    if chosen is None and by_category:
        chosen = max(by_category, key=lambda c: by_category[c] or 0)
    if chosen is None:
        return None
    try:
        monthly = float(by_category[chosen]) / months
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    estimate = round(monthly * 0.10 / 10) * 10
    return int(estimate) if estimate >= 10 else None


def _local_bill_analysis(context: dict) -> dict:
    """Exact per-bill analysis used when the model quotes an unverified figure."""
    anomaly = context.get("anomaly") or {}
    forecast = context.get("forecast") or {}
    unit_rate = context.get("unit_rate") or {}
    verdict = analysis.local_verdict(context)
    points = []
    if anomaly.get("reason"):
        points.append(anomaly["reason"])
    if forecast.get("amount"):
        points.append(f"Next month is forecast at about {_money(forecast['amount'])}.")
    if unit_rate.get("current") is not None:
        points.append(f"Your rate this month is about ₹{unit_rate['current']:,.2f} per unit.")
    tips = [verdict["top_tip"]] if verdict.get("top_tip") else []
    questions = []
    if (context.get("bill") or {}).get("units"):
        questions.append("Check the meter reading and units against the bill.")
    return {
        "summary": verdict["summary"],
        "verdict": "high" if anomaly.get("is_anomaly") else "normal",
        "key_points": points[:3],
        "savings_tips": tips[:3],
        "questions": questions[:2],
        "source": "local-fallback",
        "fallback": True,
    }


@router.post("/ai/chat")
def ai_chat(data: ChatIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Ask Smart Bill AI a free-form question about bills and savings.

    The model writes every reply from an exact fact sheet, and Python verifies the
    figures before the answer is returned.
    """
    rate_limit(f"chat:{user.id}", 20, 60)
    context = _chat_context(db, user)
    history = [turn.model_dump() for turn in data.history]
    try:
        answer = _model_reply(data.question, context, history)
    except llm.LLMError as e:
        log.warning("chat AI failed for user %s: %s", user.id, e)
        raise _fail(e)
    return {"answer": answer}


@router.post("/ai/chat/stream")
def ai_chat_stream(data: ChatIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Same answer as /ai/chat, streamed line-by-line so the UI shows text immediately.

    The answer is verified in full before the first token is sent, so a wrong figure
    can never reach the screen.
    """
    rate_limit(f"chat:{user.id}", 20, 60)
    context = _chat_context(db, user)
    history = [turn.model_dump() for turn in data.history]

    def events():
        try:
            answer = _model_reply(data.question, context, history)
            for start in range(0, len(answer), 120):
                yield json.dumps({"delta": answer[start:start + 120]}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}) + "\n"
        except llm.LLMError as e:
            log.warning("streaming chat failed for user %s: %s", user.id, e)
            yield json.dumps({"error": str(e)}, ensure_ascii=False) + "\n"
        except Exception:
            log.exception("streaming chat crashed for user %s", user.id)
            yield json.dumps({"error": "Smart Bill AI could not finish that answer. Please try again."}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

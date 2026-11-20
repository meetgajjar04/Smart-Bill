"""Regressions for the production-handoff backend correctness fixes."""
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from .conftest import H, make_user
from app.database import SessionLocal
from app.models import Alert, Bill, User
from app.services import ai_cache, bulk, emailer

INTERNAL = {"X-Internal-Key": "dev-internal-key", **H}


def _bill_payload(due_in=1, amount=1200, category="electricity"):
    due = date.today() + timedelta(days=due_in)
    return {"category": category, "provider": "Test Provider", "bill_date": (due - timedelta(days=15)).isoformat(),
            "due_date": due.isoformat(), "amount": amount}


def _user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def _reviewed_bill(user_id: int, *, amount=None, due_in=1) -> int:
    with SessionLocal() as db:
        bill = Bill(user_id=user_id, reviewed=True, category="electricity", provider="Legacy bill",
                    bill_date=date.today() - timedelta(days=15), due_date=date.today() + timedelta(days=due_in),
                    amount=amount)
        db.add(bill)
        db.commit()
        db.refresh(bill)
        return bill.id


def test_internal_alert_rejects_another_users_bill(client):
    owner = make_user(TestClient, "handoff-owner@example.com")
    stranger = make_user(TestClient, "handoff-stranger@example.com")
    bill_id = owner.post("/api/bills", json=_bill_payload()).json()["id"]
    payload = {"user_id": _user_id(stranger), "bill_id": bill_id, "kind": "anomaly",
               "severity": "warning", "message": "Not your bill."}
    assert client.post("/api/internal/alerts", headers=INTERNAL, json=payload).status_code == 404


def test_internal_alerts_are_deduplicated(client):
    c = make_user(TestClient, "handoff-dedupe@example.com")
    user_id, bill_id = _user_id(c), c.post("/api/bills", json=_bill_payload()).json()["id"]
    payload = {"user_id": user_id, "bill_id": bill_id, "kind": "reminder",
               "severity": "warning", "message": "Bill is due tomorrow."}
    first = client.post("/api/internal/alerts", headers=INTERNAL, json=payload)
    second = client.post("/api/internal/alerts", headers=INTERNAL, json=payload)
    assert first.json()["created"] is True and second.json()["created"] is False
    with SessionLocal() as db:
        assert db.query(Alert).filter(Alert.user_id == user_id, Alert.bill_id == bill_id).count() == 1


def test_internal_due_list_hides_opted_out_users(client):
    off = make_user(TestClient, "handoff-optout@example.com")
    off.patch("/api/auth/me", json={"notify_email": False})
    off.post("/api/bills", json=_bill_payload())
    on = make_user(TestClient, "handoff-optin@example.com")
    on.post("/api/bills", json=_bill_payload())

    rows = client.get("/api/internal/reminders/due", headers=INTERNAL, params={"days": 3}).json()["reminders"]
    assert all(r["user_id"] != _user_id(off) for r in rows)
    assert any(r["user_id"] == _user_id(on) for r in rows)


def test_null_amount_bills_do_not_break_analysis_or_summary(client):
    c = make_user(TestClient, "handoff-null@example.com")
    user_id, bill_id = _user_id(c), _reviewed_bill(_user_id(c), amount=None)

    assert c.get("/api/analytics/summary").status_code == 200
    r = client.get(f"/api/internal/bills/{bill_id}/analysis", headers=INTERNAL)
    assert r.status_code == 200 and r.json()["bill"]["amount"] is None

    result = {"is_anomaly": False, "severity": "none", "summary": "No amount recorded."}
    assert client.post(f"/api/internal/bills/{bill_id}/analysis-result",
                       headers=INTERNAL, json=result).status_code == 204

    with SessionLocal() as db:
        assert bulk.existing_keys(db, db.get(User, user_id)) == set()


def test_emailer_copes_with_a_missing_amount(monkeypatch):
    sent = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, body: sent.append((subject, body)) or True)
    user = SimpleNamespace(email="legacy@example.com", name="Legacy")
    bill = SimpleNamespace(id=1, category="electricity", provider="Test", account_number=None,
                           amount=None, due_date=date.today() + timedelta(days=1))
    assert emailer.send_due_reminder(user, bill) is True
    assert "amount not recorded" in sent[0][1]


def test_internal_reminder_sent_is_idempotent(client):
    c = make_user(TestClient, "handoff-sent@example.com")
    bill_id = c.post("/api/bills", json=_bill_payload()).json()["id"]
    url = f"/api/internal/reminders/{bill_id}/sent"
    assert client.post(url, headers=INTERNAL).status_code == 204
    assert client.post(url, headers=INTERNAL).status_code == 204
    with SessionLocal() as db:
        assert db.get(Bill, bill_id).reminded_at is not None
        assert db.query(Alert).filter(Alert.bill_id == bill_id, Alert.kind == "reminder").count() == 1


def test_payment_invalidates_both_ai_caches(client):
    c = make_user(TestClient, "handoff-payment-cache@example.com")
    user_id, bill_id = _user_id(c), c.post("/api/bills", json=_bill_payload()).json()["id"]
    with SessionLocal() as db:
        ai_cache.save(db, user_id, "portfolio", {"headline": "Old summary"})
        ai_cache.save(db, user_id, "bill", {"summary": "Old verdict"}, bill_id=bill_id)

    assert c.patch(f"/api/bills/{bill_id}/payment", json={"paid": True}).status_code == 200
    with SessionLocal() as db:
        assert ai_cache.latest(db, user_id, "portfolio") is None
        assert ai_cache.latest(db, user_id, "bill", bill_id) is None

import io
from datetime import date, timedelta

from fastapi.testclient import TestClient

from .conftest import H, make_user


def bill(cat="electricity", amount=1200, due_in=10, **kw):
    d = date.today() + timedelta(days=due_in)
    return {"category": cat, "provider": "Adani Electricity", "bill_date": (d - timedelta(days=15)).isoformat(),
            "due_date": d.isoformat(), "amount": amount, **kw}


def test_auth_flow_and_csrf(client):
    c = make_user(TestClient, "a@example.com")
    assert c.get("/api/auth/me").json()["email"] == "a@example.com"
    no_hdr = TestClient(c.app)
    no_hdr.cookies.update(c.cookies)
    assert no_hdr.post("/api/bills", json=bill()).status_code == 403  # CSRF header missing
    assert TestClient(c.app).get("/api/auth/me").status_code == 401


def test_weak_password_and_bad_login(client):
    c = TestClient(client.app, headers=H)
    assert c.post("/api/auth/register", json={"name": "x", "email": "w@example.com", "password": "short"}).status_code == 422
    assert c.post("/api/auth/login", json={"email": "nobody@example.com", "password": "Whatever123"}).status_code == 401


def test_lockout_after_repeated_failures(client):
    make_user(TestClient, "lock@example.com")
    c = TestClient(client.app, headers=H)
    codes = [c.post("/api/auth/login", json={"email": "lock@example.com", "password": "wrong-pass-1"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 423


def test_bill_crud_payment_and_validation(client):
    c = make_user(TestClient, "b@example.com")
    r = c.post("/api/bills", json=bill())
    assert r.status_code == 201 and r.json()["state"] == "unpaid"
    bid = r.json()["id"]
    assert c.post("/api/bills", json=bill(amount=-5)).status_code == 422
    assert c.patch(f"/api/bills/{bid}/payment", json={"paid": True}).json()["state"] == "paid"
    assert c.get("/api/bills?status=paid").json()["total"] == 1
    assert c.get("/api/bills?month=2026-13").status_code == 422
    assert c.delete(f"/api/bills/{bid}").status_code == 204


def test_users_cannot_access_each_others_bills(client):
    alice, mallory = make_user(TestClient, "alice@example.com"), make_user(TestClient, "mallory@example.com")
    bid = alice.post("/api/bills", json=bill()).json()["id"]
    for call in (lambda: mallory.get(f"/api/bills/{bid}"), lambda: mallory.delete(f"/api/bills/{bid}"),
                 lambda: mallory.get(f"/api/bills/{bid}/file"), lambda: mallory.put(f"/api/bills/{bid}", json=bill())):
        assert call().status_code == 404
    assert mallory.get("/api/bills").json()["total"] == 0


def test_upload_rejects_non_bills_and_oversize(client):
    c = make_user(TestClient, "up@example.com")
    r = c.post("/api/bills/extract", files={"file": ("evil.pdf", io.BytesIO(b"MZ\x90 not a pdf"), "application/pdf")})
    assert r.status_code == 415
    big = io.BytesIO(b"%PDF" + b"0" * (16 * 1024 * 1024))
    assert c.post("/api/bills/extract", files={"file": ("big.pdf", big, "application/pdf")}).status_code == 413


def test_analytics_and_anomaly_end_to_end(client):
    c = make_user(TestClient, "an@example.com")
    for i, amt in enumerate([1200, 1300, 1250, 1400, 1350, 1500, 3400]):
        d = date.today().replace(day=1) - timedelta(days=30 * (6 - i))
        c.post("/api/bills", json={"category": "electricity", "bill_date": d.isoformat(),
                                   "due_date": (d + timedelta(days=14)).isoformat(), "amount": amt, "units": amt / 7})
    p = c.get("/api/analytics/predict?category=electricity").json()
    assert p["forecast"]["amount"] > 0 and len(p["history"]) >= 6
    assert any(a["amount"] == 3400 for a in c.get("/api/analytics/anomalies").json())
    assert c.get("/api/analytics/summary").json()["unpaid_count"] >= 1
    assert c.get("/api/analytics/recommendations").status_code == 200
    # background fallback analysis ran (no n8n configured) and raised an in-app alert
    assert any(a["kind"] == "anomaly" for a in c.get("/api/alerts").json())


def test_internal_api_requires_key(client):
    assert client.get("/api/internal/reminders/due").status_code == 401
    ok = client.get("/api/internal/reminders/due", headers={"X-Internal-Key": "dev-internal-key"})
    assert ok.status_code == 200 and "reminders" in ok.json()

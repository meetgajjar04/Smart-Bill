"""Tests for e-mail reminders, unusual-bill e-mails notifying, and Excel/CSV bulk import."""
import io
from datetime import date, timedelta

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.database import SessionLocal
from app.models import Alert, Bill
from app.routers import notifications
from app.services import analysis, emailer, reminders

from .conftest import make_user


def _bill_body(*, due_offset=1, amount=2450.0, category="electricity", provider="Adani Electricity"):
    due = date.today() + timedelta(days=due_offset)
    return {"category": category, "provider": provider, "account_number": "150234567",
            "bill_date": (due - timedelta(days=14)).isoformat(), "due_date": due.isoformat(),
            "amount": amount, "units": 300}


# ------------------------------------------------------------------ bulk import
CSV_HEADER = "category,provider,account_number,bill_date,due_date,amount,units,notes\n"


def _preview(c, name, data, ctype="text/csv"):
    r = c.post("/api/bills/bulk-preview", files={"file": (name, io.BytesIO(data), ctype)})
    assert r.status_code == 200, r.text
    return r.json()


def _import(c, name, data, token, ctype="text/csv"):
    return c.post("/api/bills/bulk-import",
                  files={"file": (name, io.BytesIO(data), ctype)},
                  data={"preview_token": token})


def test_bulk_template_download(client):
    c = make_user(TestClient, "bulk-template@example.com")
    r = c.get("/api/bills/bulk-template")
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"  # a real .xlsx (zip) file
    assert "spreadsheetml" in r.headers["content-type"]


def test_bulk_preview_shows_every_row_and_saves_nothing(client):
    c = make_user(TestClient, "bulk-preview@example.com")
    rows = (
        "electricity,Adani Electricity,111,2026-11-01,2026-11-20,2584.50,312,First bill\n"
        "spaceship,Alien Co,222,2026-11-01,2026-11-20,500,10,Wrong category\n"
        "mobile,Jio,444,2026-11-01,2026-11-18,399,,Phone\n"
    )
    preview = _preview(c, "bills.csv", (CSV_HEADER + rows).encode())
    assert preview["total"] == 3
    assert (preview["ready"], preview["duplicates"], preview["invalid"]) == (2, 0, 1)
    by_row = {r["row"]: r for r in preview["rows"]}
    assert by_row[2]["status"] == "ready"
    assert by_row[2]["provider"] == "Adani Electricity"
    assert by_row[2]["amount"] == "2584.50"
    assert by_row[3]["status"] == "invalid" and "category" in by_row[3]["message"]
    assert by_row[4]["status"] == "ready"
    assert preview["token"]

    # Preview is read-only.
    assert c.get("/api/bills").json()["total"] == 0


def test_bulk_import_requires_preview_confirmation(client):
    c = make_user(TestClient, "bulk-confirm@example.com")
    payload = (CSV_HEADER + "gas,Mahanagar Gas,,2026-12-01,2026-12-05,780,,\n").encode()
    preview = _preview(c, "bills.csv", payload)

    no_token = c.post("/api/bills/bulk-import",
                      files={"file": ("bills.csv", io.BytesIO(payload), "text/csv")})
    assert no_token.status_code == 409
    wrong = _import(c, "bills.csv", payload, "not-the-token")
    assert wrong.status_code == 409
    other_file = (CSV_HEADER + "gas,Mahanagar Gas,,2026-12-01,2026-12-05,900,,\n").encode()
    changed = _import(c, "bills.csv", other_file, preview["token"])
    assert changed.status_code == 409

    assert c.get("/api/bills").json()["total"] == 0
    ok = _import(c, "bills.csv", payload, preview["token"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["created"] == 1
    assert c.get("/api/bills").json()["total"] == 1


def test_bulk_csv_import_reports_bad_rows_and_skips_duplicates(client, monkeypatch):
    c = make_user(TestClient, "bulk-csv@example.com")
    rows = (
        "electricity,Adani Electricity,111,2026-11-01,2026-11-20,2584.50,312,First bill\n"
        "spaceship,Alien Co,222,2026-11-01,2026-11-20,500,10,Wrong category\n"
        "water,BMC,333,2026-11-01,2026-11-20,not-a-number,,Bad amount\n"
        "mobile,Jio,444,2026-11-01,2026-11-18,399,,Phone\n"
        "electricity,Adani Electricity,111,2026-11-01,2026-11-20,2584.50,312,Duplicate of row 1\n"
    )
    payload = (CSV_HEADER + rows).encode()
    preview = _preview(c, "bills.csv", payload)
    assert preview["total"] == 5
    assert (preview["ready"], preview["duplicates"], preview["invalid"]) == (2, 1, 2)
    statuses = {r["row"]: r["status"] for r in preview["rows"]}
    assert statuses == {2: "ready", 3: "invalid", 4: "invalid", 5: "ready", 6: "duplicate"}

    r = _import(c, "bills.csv", payload, preview["token"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["total"] == 5 and out["created"] == 2 and out["skipped"] == 1
    assert [e["row"] for e in out["errors"]] == [3, 4]
    assert "category" in out["errors"][0]["message"]
    assert "amount" in out["errors"][1]["message"]

    listed = c.get("/api/bills").json()
    assert listed["total"] == 2
    assert {b["provider"] for b in listed["items"]} == {"Adani Electricity", "Jio"}


def test_bulk_xlsx_import_and_header_validation(client):
    c = make_user(TestClient, "bulk-xlsx@example.com")
    wb = Workbook()
    ws = wb.active
    ws.append(["category", "provider", "due_date", "amount"])
    ws.append(["gas", "Mahanagar Gas", "2026-12-05", 780])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    preview = _preview(c, "bills.xlsx", buf.getvalue(), xlsx_type)
    assert preview["ready"] == 1 and preview["rows"][0]["provider"] == "Mahanagar Gas"
    r = _import(c, "bills.xlsx", buf.getvalue(), preview["token"], xlsx_type)
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 1 and r.json()["errors"] == []

    bad = c.post("/api/bills/bulk-preview",
                 files={"file": ("bad.csv", io.BytesIO(b"provider,amount\nX,10\n"), "text/csv")})
    assert bad.status_code == 422
    assert "category" in bad.json()["detail"] and "due_date" in bad.json()["detail"]

    empty = c.post("/api/bills/bulk-preview",
                   files={"file": ("bills.txt", io.BytesIO(b"hello"), "text/plain")})
    assert empty.status_code == 422


# ------------------------------------------------------------------- reminders
def test_due_reminder_sends_once_and_creates_alert(client, monkeypatch):
    c = make_user(TestClient, "remind@example.com")
    bid = c.post("/api/bills", json=_bill_body(due_offset=1)).json()["id"]
    sent = []
    monkeypatch.setattr(reminders.emailer, "send_due_reminder",
                        lambda user, bill: sent.append((user.email, bill.id)) or True)

    with SessionLocal() as db:
        first = reminders.send_due_reminders(db)
        assert first["sent"] == 1 and first["skipped"] == 0
        bill = db.get(Bill, bid)
        assert bill.reminded_at is not None
        assert db.query(Alert).filter(Alert.bill_id == bid, Alert.kind == "reminder").count() == 1

        second = reminders.send_due_reminders(db)
        assert second["sent"] == 0 and second["skipped"] == 1

    assert sent == [("remind@example.com", bid)]


def test_reminders_respect_the_email_preference(client, monkeypatch):
    c = make_user(TestClient, "no-mail@example.com")
    assert c.patch("/api/auth/me", json={"notify_email": False}).status_code == 200
    bid = c.post("/api/bills", json=_bill_body(due_offset=1, category="water")).json()["id"]
    sent = []
    monkeypatch.setattr(reminders.emailer, "send_due_reminder",
                        lambda user, bill: sent.append(bill.id) or True)

    with SessionLocal() as db:
        reminders.send_due_reminders(db)
        assert db.get(Bill, bid).reminded_at is None
    assert bid not in sent


def test_unusual_bill_creates_alert_and_sends_email(client, monkeypatch):
    c = make_user(TestClient, "anomaly-mail@example.com")
    bid = c.post("/api/bills", json=_bill_body(due_offset=10, amount=5000)).json()["id"]
    sent = []
    monkeypatch.setattr(emailer, "send_anomaly_alert",
                        lambda user, bill, result: sent.append((user.email, bill.id, result["summary"])) or True)

    with SessionLocal() as db:
        bill = db.get(Bill, bid)
        analysis.persist_result(db, bill, {"is_anomaly": True, "severity": "high",
                                           "summary": "This bill is 80% above your usual use."})
        assert db.query(Alert).filter(Alert.bill_id == bid, Alert.kind == "anomaly").count() == 1

    assert sent and sent[0][0] == "anomaly-mail@example.com" and sent[0][1] == bid


def test_analysis_of_a_deleted_bill_is_discarded_quietly(client, monkeypatch):
    """Upload -> confirm -> delete can race the background analysis. MySQL's foreign
    key must not turn that into a logged error or a resurrected alert."""
    c = make_user(TestClient, "anomaly-deleted@example.com")
    bid = c.post("/api/bills", json=_bill_body(due_offset=10, amount=5000)).json()["id"]

    with SessionLocal() as db:
        bill = db.get(Bill, bid)  # background task holds the bill while the user deletes it

    assert c.delete(f"/api/bills/{bid}").status_code == 204

    sent = []
    monkeypatch.setattr(emailer, "send_anomaly_alert",
                        lambda user, bill, result: sent.append(bill.id) or True)

    with SessionLocal() as db:
        analysis.persist_result(db, bill, {"is_anomaly": True, "severity": "high",
                                           "summary": "This bill is 80% above your usual use."})
        assert db.query(Alert).filter(Alert.bill_id == bid).count() == 0

    assert sent == []


# ----------------------------------------------------------------- notifications
def test_email_status_and_test_send(client, monkeypatch):
    c = make_user(TestClient, "mail-status@example.com")
    status = c.get("/api/notifications/status").json()
    assert status["configured"] is False and status["preference_on"] is True
    assert status["reminder_days_before"] == 1
    # Without SMTP configuration the test button explains what to set.
    r = c.post("/api/notifications/test")
    assert r.status_code == 503 and "SMTP_HOST" in r.json()["detail"]

    monkeypatch.setattr(notifications.settings, "email_enabled", True)
    monkeypatch.setattr(notifications.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(notifications.settings, "smtp_from", "alerts@example.com")
    assert c.get("/api/notifications/status").json()["configured"] is True

    monkeypatch.setattr(notifications.emailer, "send_test", lambda user: True)
    ok = c.post("/api/notifications/test")
    assert ok.status_code == 200 and ok.json() == {"sent": True, "to": "mail-status@example.com"}

    monkeypatch.setattr(notifications.emailer, "send_test", lambda user: False)
    assert c.post("/api/notifications/test").status_code == 502

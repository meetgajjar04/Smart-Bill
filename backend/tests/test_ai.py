"""Tests for Smart Bill AI's Ollama integration. No real model/server is required: model output is mocked."""
import io
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.routers import ai as ai_router
from app.routers import bills as bills_router
from app.services import llm

from .conftest import make_user

BILL_TEXT = """ADANI ELECTRICITY MUMBAI LIMITED
Consumer No: 150234567
Bill Date: 05/10/2026
Due Date: 20 Oct 2026
Units Consumed: 312 kWh
Energy Charges 2200.00
TOTAL AMOUNT PAYABLE Rs. 2,584.50
"""

EXTRACTED = {
    "category": "electricity",
    "provider": "Adani Electricity",
    "account_number": "150234567",
    "bill_date": "2026-10-05",
    "due_date": "2026-10-20",
    "amount": 2584.5,
    "units": 312,
    "line_items": [{"label": "Energy Charges", "amount": 2200}, {"label": "Fixed Charges", "amount": 384.5}],
    "notes": "Pay by the due date to avoid disconnection.",
    "summary": "Electricity bill from Adani for 312 kWh.",
}


def bill_body(amount=1500, days_ago=30):
    d = date.today() - timedelta(days=days_ago)
    return {"category": "electricity", "provider": "Adani Electricity", "bill_date": d.isoformat(),
            "due_date": (d + timedelta(days=14)).isoformat(), "amount": amount, "units": amount / 7}


# --------------------------------------------------------------------- validation
def test_extract_fields_accepts_valid_json(monkeypatch):
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: json.dumps(EXTRACTED))
    out = llm.extract_fields(BILL_TEXT)
    assert out["category"] == "electricity"
    assert out["amount"] == 2584.5 and out["units"] == 312
    assert out["bill_date"] == "2026-10-05" and out["due_date"] == "2026-10-20"
    assert out["line_items"][0] == {"label": "Energy Charges", "amount": 2200.0}


def test_extract_fields_clamps_bad_values(monkeypatch):
    bad = {"category": "spaceship", "provider": "  Adani   Electricity  ", "amount": "Rs. 1,234.50",
           "units": "many", "bill_date": "not a date", "due_date": "31/02/2026",
           "line_items": [{"label": "x" * 200, "amount": 10}, {"label": "no amount"}],
           "summary": "short"}
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: "```json\n" + json.dumps(bad) + "\n```")
    out = llm.extract_fields(BILL_TEXT)
    assert out["category"] is None
    assert out["provider"] == "Adani Electricity"
    assert out["amount"] == 1234.5 and out["units"] is None
    assert out["bill_date"] is None and out["due_date"] is None
    assert len(out["line_items"]) == 1 and len(out["line_items"][0]["label"]) == 80


def test_bad_model_output_raises(monkeypatch):
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: "I think this is a bill.")
    with pytest.raises(llm.LLMBadResponse):
        llm.extract_fields(BILL_TEXT)


def test_analysis_validation(monkeypatch):
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: json.dumps({
        "summary": "This bill is higher than usual because of summer use.",
        "verdict": "HIGH",
        "key_points": ["312 kWh is up from 280", {"text": "Unit rate is ₹8.28"}],
        "savings_tips": ["Run the AC one hour less", "Check the meter reading"],
    }))
    out = llm.analyse_bill({"bill": {"amount": 2584.5}})
    assert out["verdict"] == "high"
    assert len(out["key_points"]) == 2 and "Unit rate" in out["key_points"][1]
    assert out["savings_tips"][0].startswith("Run the AC")


def test_portfolio_validation_and_actions(monkeypatch):
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: json.dumps({
        "headline": "Electricity is your fastest-growing cost",
        "summary": "Spending rose over the last three months.",
        "trends": ["Electricity up 18%", "Mobile flat"],
        "risks": ["One unusual July bill"],
        "actions": [{"title": "Shift AC use", "detail": "Run it an hour less", "est_monthly_saving": 450},
                    "Compare internet plans"],
    }))
    out = llm.analyse_portfolio({"by_month": []})
    assert out["headline"].startswith("Electricity")
    assert out["trends"] == ["Electricity up 18%", "Mobile flat"]
    assert out["actions"][0]["est_monthly_saving"] == 450
    assert out["actions"][1]["title"] == "Compare internet plans"


def test_status_reports_missing_model(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_enabled", True)
    monkeypatch.setattr(llm.settings, "ollama_model", "test-model")
    monkeypatch.setattr(llm, "_tags", lambda: ["llama3.1:8b"])
    st = llm.status()
    assert st["available"] is True and st["model_ready"] is False
    assert "ollama pull test-model" in st["error"]
    assert "model" not in st  # the UI never shows the internal model name

    monkeypatch.setattr(llm, "_tags", lambda: ["test-model"])
    assert llm.status()["model_ready"] is True


def test_status_auto_detects_installed_model(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_enabled", True)
    monkeypatch.setattr(llm.settings, "ollama_model", "")
    monkeypatch.setattr(llm, "_tags", lambda: [])
    st = llm.status()
    assert st["available"] is True and st["model_ready"] is False
    assert "ollama pull" in st["error"]

    monkeypatch.setattr(llm, "_tags", lambda: ["some-model:latest"])
    assert llm.status()["model_ready"] is True
    assert llm._resolved_model() == "some-model:latest"


def test_status_when_server_down(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_enabled", True)

    def down():
        raise llm.LLMUnavailable("connection refused")

    monkeypatch.setattr(llm, "_tags", down)
    st = llm.status()
    assert st["available"] is False and "connection refused" in st["error"]


def test_chat_and_analysis_use_their_own_models(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_model", "chat-model")
    monkeypatch.setattr(llm.settings, "ollama_analysis_model", "fast-model")
    assert llm._resolved_model() == "chat-model"
    assert llm._resolved_model(analysis=True) == "fast-model"
    payload = llm._chat_payload([{"role": "user", "content": "hi"}], stream=False, num_predict=10)
    assert payload["model"] == "chat-model"

    seen = []

    def fake_generate(prompt, *, system, num_predict=500, json_mode=True, analysis=False):
        seen.append(analysis)
        return json.dumps({"headline": "x", "summary": "y", "trends": [], "risks": [], "actions": []})

    monkeypatch.setattr(llm, "_generate", fake_generate)
    llm.analyse_portfolio({})
    assert seen == [True]


def test_analysis_model_falls_back_to_the_chat_model(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_enabled", True)
    monkeypatch.setattr(llm.settings, "ollama_model", "chat-model")
    monkeypatch.setattr(llm.settings, "ollama_analysis_model", "")
    assert llm.analysis_model() == "chat-model"
    assert llm._resolved_model(analysis=True) == "chat-model"
    monkeypatch.setattr(llm, "_tags", lambda: ["chat-model"])
    st = llm.status()
    assert st["model_ready"] is True and st["analysis_model_ready"] is True


def test_status_reports_a_missing_analysis_model(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_enabled", True)
    monkeypatch.setattr(llm.settings, "ollama_model", "chat-model")
    monkeypatch.setattr(llm.settings, "ollama_analysis_model", "fast-model")
    monkeypatch.setattr(llm, "_tags", lambda: ["chat-model"])
    st = llm.status()
    assert st["model_ready"] is True and st["analysis_model_ready"] is False
    assert "ollama pull fast-model" in st["error"]
    assert "fast-model" not in str({k: v for k, v in st.items() if k != "error"})

    monkeypatch.setattr(llm, "_tags", lambda: ["chat-model", "fast-model"])
    st = llm.status()
    assert st["analysis_model_ready"] is True and st["error"] is None


# ------------------------------------------------------------------- upload merge
def test_upload_uses_ai_to_fill_missing_fields(client, monkeypatch):
    c = make_user(TestClient, "ai-upload@example.com")
    monkeypatch.setattr(bills_router.settings, "ollama_enabled", True)
    monkeypatch.setattr(bills_router.settings, "ollama_extract", True)
    # The rule parser can find these, so the AI result should agree rather than override.
    monkeypatch.setattr(bills_router, "extract_text", lambda path, kind: BILL_TEXT)
    monkeypatch.setattr(llm, "extract_fields", lambda text: EXTRACTED)

    r = c.post("/api/bills/extract", files={"file": ("bill.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 80), "image/png")})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ai_used"] is True
    assert data["bill"]["provider"] == "Adani Electricity"
    assert data["bill"]["amount"] == 2584.5
    assert data["ai"]["line_items"][0]["label"] == "Energy Charges"
    assert data["bill"]["notes"].startswith("Pay by the due date")


def test_upload_survives_ai_garbage_and_fills_gaps(client, monkeypatch):
    c = make_user(TestClient, "ai-gap@example.com")
    monkeypatch.setattr(bills_router.settings, "ollama_enabled", True)
    monkeypatch.setattr(bills_router.settings, "ollama_extract", True)
    monkeypatch.setattr(bills_router, "extract_text", lambda path, kind: "Bill total 999.00")
    monkeypatch.setattr(llm, "extract_fields", lambda text: {**EXTRACTED, "provider": "MSEDCL",
                                                              "category": "electricity", "amount": 999.0,
                                                              "due_date": None})

    r = c.post("/api/bills/extract", files={"file": ("bill.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 80), "image/jpeg")})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ai_used"] is True
    assert data["bill"]["provider"] == "MSEDCL"          # rule parser found none, AI filled it
    assert data["bill"]["bill_date"] == "2026-10-05"     # AI filled the missing date
    assert data["bill"]["amount"] == 999.0               # rule amount wins


def test_upload_falls_back_when_ollama_is_down(client, monkeypatch):
    c = make_user(TestClient, "ai-down@example.com")
    monkeypatch.setattr(bills_router.settings, "ollama_enabled", True)
    monkeypatch.setattr(bills_router.settings, "ollama_extract", True)
    monkeypatch.setattr(bills_router, "extract_text", lambda path, kind: "TOTAL AMOUNT PAYABLE 1234.00")

    def down(text):
        raise llm.LLMUnavailable("Ollama is not running")

    monkeypatch.setattr(llm, "extract_fields", down)
    r = c.post("/api/bills/extract", files={"file": ("bill.jpg", io.BytesIO(b"\xff\xd8\xff" + b"1" * 80), "image/jpeg")})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ai_used"] is False and data["ai"] is None
    assert data["bill"]["amount"] == 1234.0


# ------------------------------------------------------------------ AI endpoints
def test_bill_ai_endpoint_runs_and_caches(client, monkeypatch):
    c = make_user(TestClient, "ai-bill@example.com")
    bid = c.post("/api/bills", json=bill_body()).json()["id"]

    empty = c.get(f"/api/bills/{bid}/ai-analysis")
    assert empty.status_code == 200 and empty.json()["analysis"] is None

    monkeypatch.setattr(llm, "analyse_bill", lambda ctx: {
        "summary": "Normal bill.", "verdict": "normal", "key_points": ["In line with last month"],
        "savings_tips": [], "questions": []})
    r = c.post(f"/api/bills/{bid}/ai-analysis")
    assert r.status_code == 200, r.text
    assert r.json()["analysis"]["verdict"] == "normal" and r.json()["cached"] is False

    cached = c.get(f"/api/bills/{bid}/ai-analysis")
    assert cached.json()["analysis"]["summary"] == "Normal bill." and cached.json()["cached"] is True

    # Editing the bill makes the cached analysis stale.
    c.put(f"/api/bills/{bid}", json=bill_body(amount=1700))
    assert c.get(f"/api/bills/{bid}/ai-analysis").json()["analysis"] is None


def test_bill_ai_endpoint_503_when_unavailable(client, monkeypatch):
    c = make_user(TestClient, "ai-503@example.com")
    bid = c.post("/api/bills", json=bill_body()).json()["id"]

    def down(ctx):
        raise llm.LLMUnavailable("Ollama is not reachable")

    monkeypatch.setattr(llm, "analyse_bill", down)
    r = c.post(f"/api/bills/{bid}/ai-analysis")
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]


def test_portfolio_ai_endpoint_and_ownership(client, monkeypatch):
    c = make_user(TestClient, "ai-port@example.com")
    for i in range(3):
        c.post("/api/bills", json=bill_body(amount=1200 + i * 100, days_ago=30 * (3 - i)))

    monkeypatch.setattr(llm, "analyse_portfolio", lambda ctx: {
        "headline": "Costs are steady", "summary": "Three months analysed.",
        "trends": ["Electricity rising slowly"], "risks": [], "actions": []})
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    assert r.json()["analysis"]["headline"] == "Costs are steady"
    assert c.get("/api/analytics/ai-summary").json()["cached"] is True

    other = make_user(TestClient, "ai-other@example.com")
    bid = c.post("/api/bills", json=bill_body()).json()["id"]
    assert other.get(f"/api/bills/{bid}/ai-analysis").status_code == 404
    assert other.post(f"/api/bills/{bid}/ai-analysis").status_code == 404


def test_portfolio_needs_bills(client, monkeypatch):
    c = make_user(TestClient, "ai-empty@example.com")
    monkeypatch.setattr(llm, "analyse_portfolio", lambda ctx: {"headline": "x", "summary": "y"})
    assert c.post("/api/analytics/ai-summary").status_code == 409


# ------------------------------------------------------------------------- chat
def test_chat_answers_with_household_context(client, monkeypatch):
    c = make_user(TestClient, "ai-chat@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = {}

    def fake_chat(question, context, history):
        seen.update(question=question, context=context, history=history)
        return "Your electricity bill is the biggest cost this month. Try running the AC one hour less."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "  What should my monthly budget be?  ",
                                     "history": [{"role": "user", "content": "Hi"},
                                                 {"role": "assistant", "content": "Hello"}]})
    assert r.status_code == 200, r.text
    assert r.json()["answer"].startswith("Your electricity bill")
    assert seen["question"] == "What should my monthly budget be?"
    assert seen["context"]["currency"] == "INR"
    assert seen["context"]["user_name"] == "T"
    assert seen["history"][0] == {"role": "user", "content": "Hi"}


def test_model_writes_factual_answers_from_the_exact_fact_sheet(client, monkeypatch):
    c = make_user(TestClient, "ai-quick@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = {}

    def fake_chat(question, context, history, correction=None):
        seen["question"] = question
        seen["context"] = context
        return "Your latest bill is ₹1,800 and it is due in 4 days."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "latest bill amount"})
    assert r.status_code == 200, r.text
    assert "1,800" in r.json()["answer"]
    assert seen["context"]["quick_answers"]["latest_bill"]
    assert seen["context"]["quick_answers"]["next_due_bill"]
    assert seen["context"]["quick_answers"]["most_expensive_month"]
    assert seen["context"]["recent_bills"][0]["amount"] == 1800.0


def test_latest_bill_fact_is_present_in_the_model_context(client, monkeypatch):
    c = make_user(TestClient, "ai-latest-fact@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = {}

    def fake_chat(question, context, history, correction=None):
        seen.update(context)
        return "Your latest bill is ₹1,800."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "latest bill amount"})
    assert r.status_code == 200, r.text
    assert "₹1,800" in seen["quick_answers"]["latest_bill"]
    assert seen["latest_bill"]["amount"] == 1800.0


def test_chat_validation_and_unavailable(client, monkeypatch):
    c = make_user(TestClient, "ai-chat-bad@example.com")
    assert c.post("/api/ai/chat", json={"question": "x"}).status_code == 422
    assert c.post("/api/ai/chat", json={"question": "ok", "history": [{"role": "system", "content": "x"}]}).status_code == 422

    def down(*a, **k):
        raise llm.LLMUnavailable("Ollama is not reachable")

    monkeypatch.setattr(llm, "chat_answer", down)
    r = c.post("/api/ai/chat", json={"question": "What should my monthly budget be?"})
    assert r.status_code == 503 and "not reachable" in r.json()["detail"]


def test_chat_keeps_the_last_twelve_messages(monkeypatch):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(20)]
    picked = llm._history_messages(history)
    assert len(picked) == llm.MAX_HISTORY_MESSAGES
    assert picked[-1]["content"] == "message 19"
    assert sum(len(m["content"]) for m in picked) <= llm.MAX_HISTORY_CHARS


def test_verifier_correction_reaches_the_model_in_the_user_turn():
    messages = llm._chat_messages("What is my total?", {"totals": {}}, [],
                                  correction="Use only ₹1,800.")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "What is my total?" in messages[-1]["content"]
    assert "Use only ₹1,800." in messages[-1]["content"]


def test_llm_context_stays_valid_json_within_the_budget():
    ctx = _exact_context()
    ctx["recent_bills"] = [dict(b, note="x" * 200) for b in ctx["bills"] * 40]
    ctx["months"] = [{"month": f"20{20 + i // 12:02d}-{i % 12 + 1:02d}",
                      "label": "Month", "total": 1000.0} for i in range(60)]
    ctx["unpaid_bills"] = [dict(b, note="y" * 200) for b in ctx["unpaid_bills"] * 20]
    ctx["home"] = {"address": "z" * 800}
    compact = ai_router._llm_context(ctx)
    text = json.dumps(compact, default=str, ensure_ascii=False, separators=(",", ":"))
    assert len(text) <= llm.MAX_CONTEXT_CHARS
    assert json.loads(text)["quick_answers"]["total_unpaid_inr"] == 1000.0
    # When the sheet is too large, drop the oldest months, never the newest ones.
    assert compact["months"][-1]["month"] == "2024-12"


def test_chat_stream_endpoint_sends_deltas(client, monkeypatch):
    c = make_user(TestClient, "ai-chat-stream@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = {}

    def fake_answer(question, context, history):
        seen.update(question=question, context=context, history=history)
        return "Your bill is in line with last month."

    monkeypatch.setattr(llm, "chat_answer", fake_answer)
    r = c.post("/api/ai/chat/stream", json={"question": "What should my monthly budget be?",
                                            "history": [{"role": "user", "content": "Hi"}]})
    assert r.status_code == 200, r.text
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert "".join(e.get("delta", "") for e in events) == "Your bill is in line with last month."
    assert events[-1] == {"done": True}
    assert seen["question"] == "What should my monthly budget be?" and seen["history"][0]["content"] == "Hi"


def test_chat_stream_reports_errors_as_an_event(client, monkeypatch):
    c = make_user(TestClient, "ai-chat-stream-bad@example.com")
    c.post("/api/bills", json=bill_body())

    def down(question, context, history):
        raise llm.LLMUnavailable("Ollama is not reachable")

    monkeypatch.setattr(llm, "chat_answer", down)
    r = c.post("/api/ai/chat/stream", json={"question": "What should my monthly budget be?"})
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert events[-1]["error"] == "Ollama is not reachable"


def test_model_output_cut_off_mid_json_is_repaired(monkeypatch):
    truncated = ('{"headline": "Electricity is rising", "summary": "Spending rose", '
                 '"trends": ["Electricity up 18%", "Mobile flat"], "risks": ["One unusual bill"], '
                 '"actions": [{"title": "Run the AC less", "detail": "One hour a day')
    monkeypatch.setattr(llm, "_generate", lambda *a, **k: truncated)
    out = llm.analyse_portfolio({"by_month": []})
    assert out["headline"] == "Electricity is rising"
    assert out["trends"] == ["Electricity up 18%", "Mobile flat"]
    assert out["actions"][0]["title"] == "Run the AC less"


def test_portfolio_falls_back_instead_of_erroring(client, monkeypatch):
    c = make_user(TestClient, "ai-port-fallback@example.com")
    for i in range(3):
        c.post("/api/bills", json=bill_body(amount=1200 + i * 100, days_ago=30 * (3 - i)))

    def broken(ctx):
        raise llm.LLMBadResponse("The model returned malformed JSON.")

    monkeypatch.setattr(llm, "analyse_portfolio", broken)
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    analysis = r.json()["analysis"]
    assert analysis["source"] == "local-fallback"
    assert analysis["headline"] and analysis["summary"]


def test_portfolio_rewrites_once_before_falling_back(client, monkeypatch):
    """An invented percentage triggers one verified rewrite instead of an instant fallback."""
    c = make_user(TestClient, "ai-port-repair@example.com")
    for i in range(3):
        c.post("/api/bills", json=bill_body(amount=1200 + i * 100, days_ago=30 * (3 - i)))

    calls = []

    def flaky(ctx, correction=None):
        calls.append(correction)
        if correction is None:
            return {"headline": "Electricity is 75% of your spend", "summary": "", "trends": [],
                    "risks": [], "actions": []}
        return {"headline": "Electricity is your largest cost", "summary": "Spending is steady.",
                "trends": [], "risks": [], "actions": []}

    monkeypatch.setattr(llm, "analyse_portfolio", flaky)
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    analysis = r.json()["analysis"]
    assert analysis["headline"] == "Electricity is your largest cost"
    assert "source" not in analysis  # this is the model's own verified analysis
    assert len(calls) == 2
    assert "Use only these verified facts" in calls[1]


def test_portfolio_facts_and_claim_guard():
    ctx = {
        "months_covered": 3,
        "total_spend": 3000.0,
        "by_month": [{"month": "2026-09", "total": 800.0},
                     {"month": "2026-10", "total": 1000.0},
                     {"month": "2026-11", "total": 1200.0}],
        "by_category": {"electricity": 2000.0, "water": 1000.0},
        "upcoming_bills": [{"category": "electricity", "provider": "Adani", "amount": 4151.0,
                            "due_date": "2026-11-20", "days_left": 11}],
    }
    text = " ".join(ai_router._portfolio_facts(ctx))
    assert "₹3,000" in text and "₹2,000" in text and "₹200" in text
    assert "₹4,151" in text and "2026-11-20" in text

    # A category total is not an increase.
    assert ai_router._portfolio_claim_mismatch({"headline": "Electricity rose by ₹2,000"}, ctx) is True
    assert ai_router._portfolio_claim_mismatch(
        {"headline": "Electricity has risen sharply, totaling ₹2,000"}, ctx) is True
    # Even a real month delta cannot be attributed to a category.
    assert ai_router._portfolio_claim_mismatch(
        {"trend": "Electricity totals ₹2,000, up ₹200 from the previous month."}, ctx) is True
    # A real month-on-month difference is fine.
    assert ai_router._portfolio_claim_mismatch({"headline": "Spending rose by ₹200 last month"}, ctx) is False
    # A separate sentence may quote a total safely.
    assert ai_router._portfolio_claim_mismatch(
        {"summary": "Spending rose by ₹200 last month. The highest month totals ₹1,200."}, ctx) is False
    assert ai_router._portfolio_claim_mismatch({"headline": "Electricity totals ₹2,000"}, ctx) is False
    # Three months of bills are not a year.
    assert ai_router._portfolio_claim_mismatch({"summary": "Bills total ₹3,000 over the past year"}, ctx) is True


def test_portfolio_guard_rejects_a_month_total_credited_to_a_category():
    ctx = {
        "months_covered": 19,
        "total_spend": 144837.0,
        "by_month": [{"month": "2026-10", "total": 9913.0},
                     {"month": "2026-11", "total": 5877.0},
                     {"month": "2026-12", "total": 34000.0}],
        "by_category": {"electricity": 79503.0, "internet": 30882.0,
                        "mobile": 29482.0, "water": 4970.0},
        "upcoming_bills": [{"category": "electricity", "provider": "Adani Electricity",
                            "amount": 4151.0, "due_date": "2026-11-20", "days_left": 8}],
    }
    # ₹34,000 is every bill in December, not the electricity bill.
    bad = "The highest electricity bill was ₹34,000 in December."
    assert ai_router._portfolio_claim_mismatch({"summary": bad}, ctx) is True
    assert ai_router._portfolio_text_safe(bad, ctx) is False
    # The same figure is fine when it is not credited to one category.
    assert ai_router._portfolio_claim_mismatch(
        {"summary": "The highest month totals ₹34,000 in December."}, ctx) is False
    # A real bill amount for the category and the category total stay allowed.
    assert ai_router._portfolio_text_safe(
        "Adani Electricity ₹4,151 is due on 2026-11-20 (in 8 days).", ctx) is True
    assert ai_router._portfolio_text_safe(
        "Electricity totals ₹79,503 across all its bills.", ctx) is True
    # The sanitizer drops only the unsafe sentence.
    out = ai_router._sanitize_portfolio(
        {"headline": "Your bills total ₹144,837 across 19 months",
         "summary": bad,
         "trends": ["Electricity totals ₹79,503 across all its bills."],
         "risks": [], "actions": []}, ctx)
    assert out["headline"] == "Your bills total ₹144,837 across 19 months"
    assert out["summary"] == ""
    assert out["trends"] == ["Electricity totals ₹79,503 across all its bills."]


def test_portfolio_guard_keeps_superlatives_for_the_top_category_only():
    ctx = {
        "months_covered": 19,
        "total_spend": 144837.0,
        "by_month": [{"month": "2026-11", "total": 5877.0},
                     {"month": "2026-12", "total": 34000.0}],
        "by_category": {"electricity": 79503.0, "internet": 30882.0,
                        "mobile": 29482.0, "water": 4970.0},
        "recent_bills": [{"category": "internet", "provider": "Adani", "amount": 12000.0,
                          "bill_date": "2026-11-01", "due_date": "2026-12-08", "paid": False}],
    }
    assert ai_router._portfolio_text_safe(
        "Electricity is your biggest cost at ₹79,503.", ctx) is True
    # Internet is only the second-largest category.
    assert ai_router._portfolio_text_safe(
        "Internet is your biggest cost at ₹30,882.", ctx) is False
    # A single large bill may still be called the biggest bill.
    assert ai_router._portfolio_text_safe(
        "Your biggest bill is the internet bill of ₹12,000.", ctx) is True


def test_fallback_summary_names_only_one_biggest_cost():
    ctx = {
        "months_covered": 3,
        "total_spend": 12000.0,
        "by_category": {"electricity": 9000.0, "internet": 2000.0, "water": 1000.0},
        "by_month": [{"month": "2026-10", "total": 1000.0},
                     {"month": "2026-11", "total": 1200.0}],
        "upcoming_bills": [],
    }
    out = llm.fallback_portfolio(ctx)
    assert out["trends"][0] == "Electricity is your biggest cost at ₹9,000."
    assert out["trends"][1] == "Internet costs ₹2,000 in total."
    assert not any("biggest" in t for t in out["trends"][1:])


def test_portfolio_sanitizer_keeps_only_proven_sentences():
    ctx = {
        "months_covered": 3,
        "total_spend": 3000.0,
        "by_month": [{"month": "2026-09", "total": 800.0},
                     {"month": "2026-10", "total": 1000.0},
                     {"month": "2026-11", "total": 1200.0}],
        "by_category": {"electricity": 2000.0, "water": 1000.0},
        "verified_facts": ["Spending is ₹200 higher than the previous month."],
    }
    payload = {
        "headline": "Electricity is 75% of your spend",
        "summary": "Electricity rose by ₹2,000. Total spend is ₹3,000. This is because usage rose.",
        "trends": ["Electricity totals ₹2,000", "Spending rose by ₹200 last month."],
        "risks": [],
        "actions": [{"title": "Reduce electricity usage", "detail": "Switch to a cheaper plan.",
                     "est_monthly_saving": 500}],
    }
    out = ai_router._sanitize_portfolio(payload, ctx)
    assert out["headline"] == ""
    assert out["summary"] == "Total spend is ₹3,000."
    assert out["trends"] == ["Electricity totals ₹2,000", "Spending rose by ₹200 last month."]
    assert out["actions"][0]["title"] == "Reduce electricity usage"
    assert out["actions"][0]["est_monthly_saving"] == 500


def test_portfolio_card_is_completed_from_verified_facts(client, monkeypatch):
    c = make_user(TestClient, "ai-port-fill@example.com")
    for i in range(3):
        c.post("/api/bills", json=bill_body(amount=1200 + i * 100, days_ago=30 * (3 - i)))

    def partly(ctx, correction=None):
        return {"headline": "Electricity jumped 75%", "summary": "Spending is steady.",
                "trends": [], "risks": [], "actions": []}

    monkeypatch.setattr(llm, "analyse_portfolio", partly)
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    analysis = r.json()["analysis"]
    assert analysis["summary"] == "Spending is steady."
    assert analysis["headline"]       # filled from the verified built-in summary
    assert analysis["trends"]         # filled too
    assert "source" not in analysis   # some model prose survived


def test_portfolio_facts_are_sent_to_the_model(monkeypatch):
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"headline": "ok", "summary": "ok", "trends": [], "risks": [], "actions": []})

    monkeypatch.setattr(llm, "_generate", fake_generate)
    out = llm.analyse_portfolio({"verified_facts": ["Total spend is ₹100 across 2 months."]})
    assert out["headline"] == "ok"
    assert "Verified facts" in seen["prompt"]
    assert "₹100" in seen["prompt"]



def test_portfolio_repair_still_unsafe_uses_the_builtin_summary(client, monkeypatch):
    c = make_user(TestClient, "ai-port-repair-fail@example.com")
    for i in range(3):
        c.post("/api/bills", json=bill_body(amount=1200 + i * 100, days_ago=30 * (3 - i)))

    def always_invents(ctx, correction=None):
        return {"headline": "Bills jumped 92%", "summary": "", "trends": [], "risks": [], "actions": []}

    monkeypatch.setattr(llm, "analyse_portfolio", always_invents)
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    assert r.json()["analysis"]["source"] == "unverified-fallback"

# --------------------------------------------------- exact calculation engine
def _exact_context():
    """A small, fully calculated household snapshot for engine tests."""
    bills = [
        {"id": 1, "provider": "Adani Electricity", "category": "electricity", "amount": 800.0,
         "bill_date": "2026-10-05", "due_date": "2026-10-20", "period": "2026-10", "paid": True, "days_left": -18},
        {"id": 2, "provider": "MCGM", "category": "water", "amount": 200.0,
         "bill_date": "2026-10-05", "due_date": "2026-10-20", "period": "2026-10", "paid": True, "days_left": -18},
        {"id": 3, "provider": "Adani Electricity", "category": "electricity", "amount": 300.0,
         "bill_date": "2026-11-05", "due_date": "2026-11-20", "period": "2026-11", "paid": False, "days_left": 13},
        {"id": 4, "provider": "Airtel", "category": "mobile", "amount": 700.0,
         "bill_date": "2026-11-01", "due_date": "2026-11-25", "period": "2026-11", "paid": False, "days_left": 18},
    ]
    unpaid = [bills[2], bills[3]]
    quick = {
        "total_unpaid_inr": 1000.0, "unpaid_bill_count": 2, "overdue_count": 0,
        "pay_first": "Adani Electricity (electricity) ₹300 due 2026-11-20 (13 days away)",
        "next_due": "2026-11-20 (13 days away)",
        "this_month": "2026-11", "this_month_spend_inr": 1000.0,
        "last_month": "2026-10", "last_month_spend_inr": 1000.0,
        "change_vs_last_month": "0% more than last month", "change_vs_last_month_pct": 0.0,
        "change_inr": 0.0, "largest_category_all_time": "Adani Electricity (electricity) ₹1,100",
        "biggest_unpaid_bill": "Airtel (mobile) ₹700 due 2026-11-25 (18 days away)",
        "average_monthly_spend_inr": 1000.0, "all_time_spend_inr": 2000.0,
        "paid_total_inr": 1000.0, "paid_bill_count": 2, "months_recorded": 2,
    }
    return {
        "currency": "INR", "today": "2026-11-07", "user_name": "T",
        "quick_answers": quick,
        "totals": {"all_time": 2000.0, "unpaid": 1000.0, "paid": 1000.0, "bill_count": 4,
                   "unpaid_count": 2, "paid_count": 2, "months_recorded": 2, "overdue_count": 0},
        "months": [{"month": "2026-10", "label": "October 2026", "total": 1000.0},
                   {"month": "2026-11", "label": "November 2026", "total": 1000.0}],
        "categories": {
            "electricity": {"total": 1100.0, "count": 2, "average_bill": 550.0, "months": 2},
            "mobile": {"total": 700.0, "count": 1, "average_bill": 700.0, "months": 1},
            "water": {"total": 200.0, "count": 1, "average_bill": 200.0, "months": 1},
        },
        "bills": bills,
        "unpaid_bills": unpaid,
        "latest_per_category": [
            {"category": "electricity", "provider": "Adani Electricity", "amount": 300.0,
             "bill_date": "2026-11-05", "due_date": "2026-11-20", "paid": False,
             "typical_month": 800.0, "vs_typical_pct": -62.5, "all_time_total": 1100.0,
             "bill_count": 2, "average_bill": 550.0},
            {"category": "mobile", "provider": "Airtel", "amount": 700.0,
             "bill_date": "2026-11-01", "due_date": "2026-11-25", "paid": False,
             "typical_month": None, "all_time_total": 700.0, "bill_count": 1, "average_bill": 700.0},
            {"category": "water", "provider": "MCGM", "amount": 200.0,
             "bill_date": "2026-10-05", "due_date": "2026-10-20", "paid": True,
             "typical_month": None, "all_time_total": 200.0, "bill_count": 1, "average_bill": 200.0},
        ],
        "home": {},
    }


def test_exact_engine_keeps_entity_and_time_scope_separate():
    ctx = _exact_context()
    answer = ai_router._quick_reply("How much did I spend on electricity last month₹", ctx)
    assert "₹800" in answer and "Electricity" in answer
    answer = ai_router._quick_reply("How much did I spend last month₹", ctx)
    assert "₹1,000" in answer


def test_exact_engine_does_not_confuse_due_bills_with_spending():
    answer = ai_router._quick_reply("Which bills are due this month₹", _exact_context())
    assert answer.startswith("2 bill(s) due November 2026")
    assert "₹1,000" in answer


def test_exact_engine_counts_paid_and_months_correctly():
    ctx = _exact_context()
    answer = ai_router._quick_reply("How many bills did I pay₹", ctx)
    assert "2 bill(s)" in answer and "₹1,000" in answer
    answer = ai_router._quick_reply("How many months of bills do I have₹", ctx)
    assert "2 month(s)" in answer and "4 bills" in answer


def test_exact_engine_reports_total_paid_separately():
    answer = ai_router._quick_reply("What's my total paid amount₹", _exact_context())
    assert answer.startswith("You have paid ₹1,000")
    assert "unpaid balance is ₹1,000" in answer


def test_exact_engine_averages_a_category_and_considers_paid_bills():
    ctx = _exact_context()
    answer = ai_router._quick_reply("What's my average electricity bill₹", ctx)
    assert "₹550" in answer and "2 bills" in answer
    answer = ai_router._quick_reply("What's my biggest bill₹", ctx)
    assert "₹800" in answer and "paid" in answer


def test_numeric_guard_rejects_invented_figures():
    ctx = _exact_context()
    exact = ai_router._quick_reply("How much do I owe₹", ctx)
    assert ai_router._answer_numbers_safe(exact, ctx)
    assert not ai_router._answer_numbers_safe("You owe ₹99,999 across 2 bills.", ctx)
    assert not ai_router._answer_numbers_safe("Electricity is 12.5% of your spending.", ctx)
    assert not ai_router._answer_numbers_safe("The due date is 2027-01-01.", ctx)
    assert not ai_router._answer_numbers_safe("You will spend ₹1,000 in 2030.", ctx)


def test_numeric_guard_checks_bare_numbers_and_never_crashes():
    ctx = _exact_context()
    extras = ai_router._verified_extras("difference between 7894 and 5879")
    assert ai_router._answer_numbers_safe("The difference is 2,015.", ctx, extras)
    assert ai_router._answer_numbers_safe("The difference is 2015.", ctx, extras)
    assert not ai_router._answer_numbers_safe("You owe 99999 in total.", ctx)
    # A stray rupee sign used to raise ValueError and return HTTP 500.
    assert isinstance(ai_router._answer_numbers_safe("You owe ₹, across bills.", ctx), bool)


def test_arithmetic_result_is_calculated_by_python_and_given_to_the_model(client, monkeypatch):
    c = make_user(TestClient, "ai-math-fact@example.com")
    seen = {}

    def fake_chat(question, context, history, correction=None):
        seen["math"] = context.get("math_answer")
        return "7,894 - 5,879 = 2,015"

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "difference between 7894 and 5879"})
    assert r.status_code == 200, r.text
    assert "2,015" in r.json()["answer"]
    assert seen["math"] == "7,894 - 5,879 = 2,015"


def test_safe_summary_is_exact_and_passes_the_guard():
    ctx = _exact_context()
    summary = ai_router._safe_summary(ctx)
    assert "₹2,000" in summary and "₹1,000" in summary
    assert ai_router._answer_numbers_safe(summary, ctx)


def test_model_is_asked_to_repair_an_unverified_figure(client, monkeypatch):
    c = make_user(TestClient, "ai-guard@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = []

    def fake_chat(question, context, history, correction=None):
        seen.append(correction)
        if correction is None:
            return "Your bills are ₹99,999. Try saving more."
        return "Your bills total ₹1,800. Try saving more."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "How much do I owe?"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "99,999" not in answer
    assert "1,800" in answer
    assert seen[0] is None and seen[1]


def test_advice_question_drops_the_unsafe_sentence_without_a_second_model_call(client, monkeypatch):
    c = make_user(TestClient, "ai-advice-fast@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    calls = []

    def fake_chat(question, context, history, correction=None):
        calls.append(correction)
        return "Turn off unused lights. This saves ₹9,999 a year."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "How can I reduce my electricity bill?"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert len(calls) == 1  # no slow rewrite pass for advice
    assert "9,999" not in answer
    assert "lights" in answer


def _pay_first_bills(c):
    today = date.today()
    c.post("/api/bills", json={"category": "electricity", "provider": "Adani Electricity",
                               "bill_date": (today - timedelta(days=10)).isoformat(),
                               "due_date": (today + timedelta(days=4)).isoformat(), "amount": 1200})
    c.post("/api/bills", json={"category": "mobile", "provider": "Airtel",
                               "bill_date": (today - timedelta(days=5)).isoformat(),
                               "due_date": (today + timedelta(days=30)).isoformat(), "amount": 5000})


def test_pay_first_intent_is_only_added_for_pay_first_questions():
    ctx = _exact_context()
    intent = ai_router._intent_answer("Which bill should I pay first?", ctx)
    assert "Adani Electricity" in intent and "earliest due date" in intent
    assert "Airtel" in intent  # explains why the biggest bill is not first
    assert ai_router._intent_answer("When is my next bill due?", ctx) == ""
    assert ai_router._intent_answer("How much do I owe in total?", ctx) == ""


def test_month_bill_intent_returns_the_real_rows_and_a_table():
    ctx = _exact_context()
    facts = ai_router._month_bill_facts("give me october 2026 bills in a table", ctx)
    assert facts and facts["period"] == "2026-10" and facts["total"] == 1000.0
    assert [b["provider"] for b in facts["rows"]] == ["Adani Electricity", "MCGM"]

    intent = ai_router._intent_answer("give me october 2026 bills in a table", ctx)
    assert "October 2026" in intent and "₹1,000" in intent
    assert "| Provider | Category | Amount | Due date | Status |" in intent
    assert "| Adani Electricity |" in intent and "| MCGM |" in intent
    assert "| **Total** | | **₹1,000** | | |" in intent
    # The deterministic table must itself pass the numeric guard.
    assert ai_router._answer_numbers_safe(intent, ctx)


def test_month_table_guard_rejects_missing_data_and_a_missing_table():
    ctx = _exact_context()
    question = "give me october 2026 bills in a table"
    intent = ai_router._intent_answer(question, ctx)
    table = intent.split("\n\n", 1)[1]

    assert ai_router._intent_mismatch(question, "No data is available for October 2026.", ctx)
    assert ai_router._intent_mismatch(question, "There are no bills recorded for that month.", ctx)
    assert ai_router._intent_mismatch(question, "October 2026 has 2 bills totalling ₹1,000.", ctx)
    assert ai_router._intent_mismatch(question, "November 2026: 2 bills totalling ₹1,000.", ctx)
    assert ai_router._intent_mismatch(question, table, ctx) is False
    assert ai_router._intent_mismatch(question, "Here are the bills:\n\n" + table, ctx) is False


def test_missing_month_is_never_given_an_invented_table():
    ctx = _exact_context()
    question = "show me august 2025 bills in a table"
    intent = ai_router._intent_answer(question, ctx)
    assert "do not have any bills recorded for August 2025" in intent
    assert "| Provider |" not in intent
    assert ai_router._intent_mismatch(
        question, "| Provider | Amount |\n| --- | --- |\n| Adani Electricity | ₹800 |", ctx)


def test_month_comparison_is_not_replaced_by_a_single_month_table():
    ctx = _exact_context()
    question = "compare September, October and November 2026 bills"
    assert ai_router._month_bill_facts(question, ctx) is None
    assert ai_router._intent_answer(question, ctx) == ""


def test_month_table_question_is_answered_from_verified_rows_without_the_model(
        client, monkeypatch):
    c = make_user(TestClient, "ai-month-table@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=5))
    calls = []

    def fake_chat(*args, **kwargs):
        calls.append(kwargs.get("correction"))
        return "No data is available for that month in the household records."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    month = date.today().strftime("%B %Y")
    r = c.post("/api/ai/chat", json={"question": f"give me {month} bill in table format"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "No data" not in answer
    assert "| Provider | Category | Amount | Due date | Status |" in answer
    assert "1,800" in answer
    assert calls == []  # the exact table needs no model pass at all


def test_pay_first_question_names_the_soonest_bill_even_when_the_model_picks_the_biggest(
        client, monkeypatch):
    c = make_user(TestClient, "ai-pay-first@example.com")
    _pay_first_bills(c)
    seen = []

    def fake_chat(question, context, history, correction=None):
        seen.append({"intent": context.get("intent_answer"), "correction": correction})
        if correction is None:
            return "Pay the Airtel bill first because it is the most expensive."
        return "Pay the Adani Electricity bill first: ₹1,200 due in 4 days."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "Which bill should I pay first?"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "Adani" in answer
    assert seen[0]["intent"] and "Adani" in seen[0]["intent"]
    assert seen[1]["correction"] and "Adani" in seen[1]["correction"]


def test_correct_pay_first_answer_skips_the_repair_pass(client, monkeypatch):
    c = make_user(TestClient, "ai-pay-first-ok@example.com")
    _pay_first_bills(c)
    calls = []

    def fake_chat(question, context, history, correction=None):
        calls.append(correction)
        return "Pay the Adani Electricity bill first: ₹1,200 due in 4 days."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "Which bills should I pay now?"})
    assert r.status_code == 200, r.text
    assert calls == [None]
    assert "Adani" in r.json()["answer"]


def test_unverified_figures_are_never_shown_even_after_a_repair(client, monkeypatch):
    c = make_user(TestClient, "ai-guard-hard@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))

    monkeypatch.setattr(llm, "chat_answer",
                        lambda *a, **k: "Your bills are ₹99,999. Try saving more.")
    r = c.post("/api/ai/chat", json={"question": "Tell me about my household."})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "99,999" not in answer
    assert answer.strip()


def test_balance_question_is_answered_by_the_model(client, monkeypatch):
    c = make_user(TestClient, "ai-numeric-fallback@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))

    monkeypatch.setattr(llm, "chat_answer",
                        lambda *a, **k: "Your outstanding balance is ₹1,800 across 1 unpaid bill.")
    r = c.post("/api/ai/chat", json={"question": "What is my balance?"})
    assert r.status_code == 200, r.text
    assert "1,800" in r.json()["answer"]


def test_exact_engine_understands_balance_phrasings():
    ctx = _exact_context()
    for question in ("How much is left to pay?",
                     "What's my outstanding balance?",
                     "How much do I need to pay?",
                     "What do I owe?"):
        answer = ai_router._quick_reply(question, ctx)
        assert answer and "₹1,000" in answer and "2 unpaid bill" in answer, question


def test_exact_engine_understands_paid_and_monthly_phrasings():
    ctx = _exact_context()
    answer = ai_router._quick_reply("How much have I paid?", ctx)
    assert "You have paid ₹1,000" in answer
    answer = ai_router._quick_reply("How much did I pay last month?", ctx)
    assert "In October 2026 you paid ₹1,000" in answer
    answer = ai_router._quick_reply("What's my average monthly spend?", ctx)
    assert "₹1,000" in answer and "2 month(s)" in answer
    answer = ai_router._quick_reply("What's my monthly electricity bill?", ctx)
    assert "₹550" in answer and "2 month(s)" in answer


def test_exact_engine_understands_superlative_bill_and_provider_phrasings():
    ctx = _exact_context()
    answer = ai_router._quick_reply("Which bill costs the most?", ctx)
    assert "₹800" in answer
    answer = ai_router._quick_reply("Which bill costs the least?", ctx)
    assert "₹200" in answer
    answer = ai_router._quick_reply("Which provider do I pay the most?", ctx)
    assert "Adani Electricity" in answer and "₹1,100" in answer


def test_exact_engine_understands_due_date_phrasings():
    ctx = _exact_context()
    answer = ai_router._quick_reply("Which bill is due next?", ctx)
    assert "₹300" in answer and "2026-11-20" in answer
    answer = ai_router._quick_reply("What is the last date to pay my water bill?", ctx)
    assert "MCGM" in answer and "2026-10-20" in answer


def test_calculator_answers_plain_arithmetic_exactly():
    assert ai_router._math_reply("what is 7894*5879") == "7894*5879 = 46,408,826"
    assert ai_router._math_reply("2 + 2") == "2+2 = 4"
    assert ai_router._math_reply("what is 18% of 4151") == "18% of 4,151 is 747.18."
    assert "divide by zero" in ai_router._math_reply("10 / 0")
    assert ai_router._math_reply("what is my electricity bill?") is None
    assert ai_router._math_reply("how much do I owe?") is None


def test_model_answers_maths_and_the_guard_verifies_it(client, monkeypatch):
    c = make_user(TestClient, "ai-calculator@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))

    monkeypatch.setattr(llm, "chat_answer",
                        lambda *a, **k: "7894*5879 = 46,408,826")
    r = c.post("/api/ai/chat", json={"question": "what is 7894*5879"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "7894*5879 = 46,408,826"
    r = c.post("/api/ai/chat/stream", json={"question": "what is 7894*5879"})
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert "".join(e.get("delta", "") for e in events) == "7894*5879 = 46,408,826"


def test_wrong_model_arithmetic_is_repaired(client, monkeypatch):
    c = make_user(TestClient, "ai-calculator-wrong@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))
    seen = []

    def fake_chat(question, context, history, correction=None):
        seen.append(correction)
        if correction:
            return "7894*5879 = 46,408,826"
        return "7894*5879 = 46,000,000"

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "what is 7894*5879"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "7894*5879 = 46,408,826"
    assert seen[1] and "46,408,826" in seen[1]


def test_percentage_question_is_checked_against_python(client, monkeypatch):
    c = make_user(TestClient, "ai-percent@example.com")
    c.post("/api/bills", json=bill_body(amount=1800, days_ago=10))

    def fake_chat(question, context, history, correction=None):
        return "18% of 4,151 is 747.18."

    monkeypatch.setattr(llm, "chat_answer", fake_chat)
    r = c.post("/api/ai/chat", json={"question": "what is 18% of 4151"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "18% of 4,151 is 747.18."


def test_analysis_guard_rejects_invented_figures_and_accepts_computed_ones():
    ctx = {
        "total_spend": 1000.0,
        "months_covered": 2,
        "by_category": {"electricity": 1000.0},
        "by_month": [{"month": "2026-10", "total": 400.0}, {"month": "2026-11", "total": 600.0}],
        "today": "2026-11-07",
    }
    safe = {"headline": "Electricity is 100% of your spend.",
            "summary": "Spending is up 50% in November 2026.",
            "trends": ["Electricity costs ₹1,000."], "risks": [], "actions": []}
    assert ai_router._analysis_payload_safe(safe, ctx)
    assert not ai_router._analysis_payload_safe({"headline": "Electricity costs ₹99,999."}, ctx)
    assert not ai_router._analysis_payload_safe({"summary": "Spending rose 73%."}, ctx)
    assert not ai_router._analysis_payload_safe({"summary": "Due on 2027-01-01."}, ctx)

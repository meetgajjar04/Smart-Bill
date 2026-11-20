"""Production-handoff regressions: calculator DoS guard and AI result provenance."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.routers import ai as ai_router
from app.services import ai_cache, llm

from .conftest import make_user


def _bill_body(amount=1500):
    d = date.today() - timedelta(days=30)
    return {"category": "electricity", "provider": "Adani Electricity", "bill_date": d.isoformat(),
            "due_date": (d + timedelta(days=14)).isoformat(), "amount": amount, "units": amount / 7}


def _user_id(c):
    return c.get("/api/auth/me").json()["id"]


# ---------------------------------------------------------------- arithmetic guard
def test_nested_and_huge_powers_are_rejected():
    # The right operand used to be a BinOp, so the old exponent cap missed these
    # and eval tried to build integers large enough to exhaust the server.
    assert ai_router._calculate("9**9**9") is None
    assert ai_router._calculate("2**(10**100)") is None
    assert ai_router._calculate("(9**9)**9") is None
    assert ai_router._calculate("2**(9+9)") is None
    assert ai_router._calculate("10**101") is None
    assert ai_router._calculate("1" + "0" * 400 + "+1") is None  # huge literal constant

    assert ai_router._math_reply("what is 9^9^9") is None
    assert ai_router._math_reply("what is 2 to the power of 9 to the power of 9") is None
    assert ai_router._math_reply("what is 2 to the power of 10") == "2^10 = 1,024"
    assert ai_router._math_reply("what is 2^10") == "2^10 = 1,024"
    assert ai_router._math_reply("what is 7894*5879") == "7894*5879 = 46,408,826"


def test_calculator_survives_memory_error(monkeypatch):
    def no_memory(*args, **kwargs):
        raise MemoryError("out of memory")

    monkeypatch.setattr(ai_router, "eval", no_memory, raising=False)
    assert ai_router._calculate("2+2") is None


# ------------------------------------------------------------- provenance / caching
def test_bill_ai_local_fallback_is_marked_in_response_and_cache(client, monkeypatch):
    c = make_user(TestClient, "handoff-bill-fallback@example.com")
    uid = _user_id(c)
    bid = c.post("/api/bills", json=_bill_body()).json()["id"]

    empty = c.get(f"/api/bills/{bid}/ai-analysis").json()
    assert empty["analysis"] is None and empty["cached"] is False

    def invented(ctx):
        return {"summary": "This bill is ₹999999.00 and unusual.", "verdict": "high",
                "key_points": [], "savings_tips": [], "questions": []}

    monkeypatch.setattr(llm, "analyse_bill", invented)
    r = c.post(f"/api/bills/{bid}/ai-analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cached"] is False
    assert body["source"] == "local-fallback" and body["fallback"] is True
    assert body["analysis"]["source"] == "local-fallback"

    cached = c.get(f"/api/bills/{bid}/ai-analysis").json()
    assert cached["cached"] is True
    assert cached["source"] == "local-fallback" and cached["fallback"] is True
    assert cached["analysis"]["source"] == "local-fallback"
    assert cached["analysis"]["fallback"] is True

    with SessionLocal() as db:
        row = ai_cache.latest(db, uid, "bill", bid)
        assert row is not None and row.model == "local-fallback"


def test_bill_ai_model_output_is_reported_as_model(client, monkeypatch):
    c = make_user(TestClient, "handoff-bill-model@example.com")
    uid = _user_id(c)
    bid = c.post("/api/bills", json=_bill_body()).json()["id"]

    monkeypatch.setattr(llm, "analyse_bill", lambda ctx: {
        "summary": "Normal bill.", "verdict": "normal", "key_points": [],
        "savings_tips": [], "questions": []})
    r = c.post(f"/api/bills/{bid}/ai-analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "model" and body["fallback"] is False
    assert "source" not in body["analysis"]

    cached = c.get(f"/api/bills/{bid}/ai-analysis").json()
    assert cached["cached"] is True and cached["source"] == "model" and cached["fallback"] is False
    with SessionLocal() as db:
        assert ai_cache.latest(db, uid, "bill", bid).model != "local-fallback"


def test_portfolio_fallback_provenance_and_cached_flag(client, monkeypatch):
    c = make_user(TestClient, "handoff-port-fallback@example.com")
    uid = _user_id(c)
    for i in range(3):
        c.post("/api/bills", json=_bill_body(amount=1200 + i * 100))

    def too_slow(ctx, correction=None):
        raise llm.LLMTimeout("too slow")

    monkeypatch.setattr(llm, "analyse_portfolio", too_slow)
    r = c.post("/api/analytics/ai-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "local-fallback" and body["fallback"] is True
    assert body["analysis"]["source"] == "local-fallback"

    cached = c.get("/api/analytics/ai-summary").json()
    assert cached["cached"] is True and cached["source"] == "local-fallback"
    assert cached["analysis"]["fallback"] is True
    with SessionLocal() as db:
        assert ai_cache.latest(db, uid, "portfolio").model == "local-fallback"


def test_cache_invalidation_helpers_and_explicit_provenance(client):
    c = make_user(TestClient, "handoff-cache@example.com")
    uid = _user_id(c)
    bid = c.post("/api/bills", json=_bill_body()).json()["id"]

    with SessionLocal() as db:
        ai_cache.save(db, uid, "bill", {"summary": "x"}, bill_id=bid, model="model-A")
        ai_cache.save(db, uid, "portfolio", {"headline": "x"}, model="model-A")
        assert ai_cache.latest(db, uid, "bill", bid).model == "model-A"

        ai_cache.invalidate_portfolio(db, uid)
        assert ai_cache.latest(db, uid, "portfolio") is None
        assert ai_cache.latest(db, uid, "bill", bid) is not None

        ai_cache.clear_bill(db, uid, bid)
        assert ai_cache.latest(db, uid, "bill", bid) is None

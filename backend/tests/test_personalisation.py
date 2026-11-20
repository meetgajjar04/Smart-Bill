from datetime import date, timedelta

import numpy as np
from fastapi.testclient import TestClient

from app.services import ml, profile_model as pm
from .conftest import make_user

HOME = dict(city="Mumbai", area_sqft=900, occupants=3, ac_count=2, ac_hours_peak=6, electric_geyser=True, solar_kw=0)


def test_simulator_behaves_physically():
    base = dict(HOME, climate_zone="hot_dry")
    assert pm.simulate_kwh({**base, "ac_hours_peak": 10}, 5) > pm.simulate_kwh(base, 5) > pm.simulate_kwh({**base, "ac_count": 0}, 5)
    assert pm.simulate_kwh({**base, "solar_kw": 3}, 4) < pm.simulate_kwh(base, 4)
    assert pm.simulate_kwh(base, 5) > pm.simulate_kwh(base, 1)  # summer > winter for a hot_dry home with ACs
    assert pm.simulate_kwh({**base, "electric_geyser": 1}, 1) > pm.simulate_kwh({**base, "electric_geyser": 0}, 1)


def test_gbm_prior_tracks_the_simulator():
    got = pm.kwh_by_month(HOME)
    sim = {m: pm.simulate_kwh(HOME, m) for m in range(1, 13)}
    assert np.mean([abs(got[m] - sim[m]) / sim[m] for m in sim]) < 0.15


def test_amount_from_kwh_is_progressive():
    assert pm.amount_from_kwh(400) / 400 > pm.amount_from_kwh(100) / 100
    assert pm.amount_from_kwh(100, 8.0, 50) == 850


def test_calibration_shrinks_toward_one():
    prior = lambda m: 1000.0
    assert ml.calibration_factor([], [], prior) == 1.0
    one = ml.calibration_factor([2000], [5], prior)
    many = ml.calibration_factor([2000] * 8, list(range(8)), prior)
    assert 1.0 < one < many <= 2.0


def test_cold_start_and_learning_from_history():
    prior = pm.make_prior(HOME)
    cold = ml.ensemble_forecast([], [], 700, prior)
    assert cold["amount"] > 0 and cold["n_months"] == 0 and cold["models"][0]["key"] == "profile"
    # a household that really runs 40% above the profile estimate: forecast must move toward it
    ords = list(range(690, 696))
    hist = [prior(o % 12 + 1) * 1.4 for o in ords]
    warm = ml.ensemble_forecast(hist, ords, 696, prior)
    truth = prior(696 % 12 + 1) * 1.4
    assert abs(warm["amount"] - truth) < abs(prior(696 % 12 + 1) - truth) * 0.4
    assert warm["low"] < warm["amount"] < warm["high"] and warm["mape"] is not None


def test_ensemble_without_profile_still_works():
    assert ml.ensemble_forecast([], [], 10, None) is None
    f = ml.ensemble_forecast([500, 520, 510, 530, 540], [1, 2, 3, 4, 5], 6, None)
    assert 450 < f["amount"] < 650


def test_profile_api_and_questions(client):
    c = make_user(TestClient, "p1@example.com")
    r = c.get("/api/profile").json()
    assert r["completeness"] == 0 and "city" in r["unanswered"]
    r = c.put("/api/profile", json=HOME).json()
    assert 40 < r["completeness"] < 100 and r["climate_zone"] == "hot_humid" and "city" not in r["unanswered"]
    assert c.put("/api/profile", json={"occupants": 0}).status_code == 422
    assert c.put("/api/profile", json={"ac_hours_peak": 99}).status_code == 422


def test_forecast_from_profile_alone_and_what_if(client):
    c = make_user(TestClient, "p2@example.com")
    assert c.post("/api/analytics/what-if", json={"ac_hours_peak": 3}).status_code == 409  # no profile yet
    c.put("/api/profile", json=HOME)
    p = c.get("/api/analytics/predict?category=electricity").json()
    assert p["profile_used"] and p["forecast"]["amount"] > 0 and p["forecast"]["n_months"] == 0 and p["drivers"]
    w = c.post("/api/analytics/what-if", json={"ac_hours_peak": 3}).json()
    assert w["annual_saving"] > 0 and len(w["months"]) == 12
    assert c.post("/api/analytics/what-if", json={}).status_code == 422


def test_feedback_excludes_temporary_spike_and_resets_for_permanent(client):
    c = make_user(TestClient, "p3@example.com")
    ids = []
    for i, amt in enumerate([1200, 1300, 1250, 1400, 3500]):
        d = date.today().replace(day=1) - timedelta(days=30 * (4 - i))
        r = c.post("/api/bills", json={"category": "electricity", "bill_date": d.isoformat(),
                                       "due_date": (d + timedelta(days=14)).isoformat(), "amount": amt})
        ids.append(r.json()["id"])
    spike = ids[-1]
    before = c.get("/api/analytics/predict?category=electricity").json()["forecast"]["amount"]
    r = c.post(f"/api/bills/{spike}/feedback", json={"reason": "guests"}).json()
    assert r["kind"] == "temporary"
    after = c.get("/api/analytics/predict?category=electricity").json()
    assert after["forecast"]["amount"] < before and any(h["excluded"] for h in after["history"])
    assert c.get("/api/alerts").json() == [] or all(a["bill_id"] != spike for a in c.get("/api/alerts").json())
    assert c.post(f"/api/bills/{spike}/feedback", json={"reason": "bogus"}).status_code == 422
    other = make_user(TestClient, "p3b@example.com")
    assert other.post(f"/api/bills/{spike}/feedback", json={"reason": "guests"}).status_code == 404

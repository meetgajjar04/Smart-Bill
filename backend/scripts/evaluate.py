"""Backtest the forecasting stack on simulated households:  python -m scripts.evaluate [n_users]
Truth = physics simulator x hidden habit factor x tariff mismatch x slow growth x noise. The *reported* profile is
deliberately imperfect (people misjudge AC hours, area...). Compares next-month MAPE by months of history."""
import sys

import numpy as np

from app.services import ml, profile_model as pm

rng = np.random.default_rng(42)
ZONES = list(pm.COOL)


def make_user():
    ac = int(rng.choice([0, 1, 2, 3], p=[.15, .4, .3, .15]))
    true = dict(area_sqft=int(np.clip(rng.lognormal(6.8, .35), 400, 3500)), occupants=int(rng.integers(1, 7)), ac_count=ac,
                ac_hours_peak=float(rng.integers(3, 12)) if ac else 0.0, fridges=1, electric_geyser=int(rng.random() < .45),
                induction=int(rng.random() < .2), washing_machine=1, water_pump=int(rng.random() < .2), ev_km_per_day=0.0,
                solar_kw=0.0, wfh_days=int(rng.integers(0, 5)), climate_zone=str(rng.choice(ZONES)))
    reported = {**true, "area_sqft": int(true["area_sqft"] * rng.uniform(.85, 1.15)),
                "occupants": int(np.clip(true["occupants"] + rng.integers(-1, 2), 1, 8)),
                "ac_hours_peak": float(np.clip(true["ac_hours_peak"] + rng.uniform(-2, 2), 0, 24)) if ac else 0.0}
    k, tariff, growth = rng.lognormal(0, .18), rng.uniform(.8, 1.25), rng.uniform(-.003, .008)
    start = int(rng.integers(0, 12))
    series, ords = [], []
    for t in range(14):
        month = (start + t) % 12 + 1
        kwh = pm.simulate_kwh(true, month) * k * (1 + growth) ** t * rng.lognormal(0, .07)
        series.append(pm.amount_from_kwh(kwh) * tariff)
        ords.append(start + t)
    return reported, series, ords


def main(n_users=200):
    pm.load_prior()
    steps = [0, 1, 3, 6, 12]
    err = {s: {"naive": [], "trend": [], "profile (raw)": [], "hybrid": []} for s in steps}
    for _ in range(n_users):
        rep, y, ords = make_user()
        prior = pm.make_prior(rep)
        for n in steps:
            actual, o = y[n], ords[n]
            pct = lambda f: abs(f - actual) / actual
            err[n]["profile (raw)"].append(pct(prior(o % 12 + 1)))
            err[n]["hybrid"].append(pct(ml.ensemble_forecast(y[:n], ords[:n], o, prior)["amount"]))
            if n >= 1:
                err[n]["naive"].append(pct(y[n - 1]))
            if n >= 3:
                err[n]["trend"].append(pct(ml.forecast_next(y[:n])["amount"]))
    print(f"Next-month forecast error (MAPE %, lower is better), {n_users} simulated households")
    print(f"{'months of history':>18} | " + " | ".join(f"{k:>13}" for k in ["naive", "trend", "profile (raw)", "hybrid"]))
    for n in steps:
        row = [f"{np.mean(err[n][k]) * 100:13.1f}" if err[n][k] else f"{'—':>13}" for k in ["naive", "trend", "profile (raw)", "hybrid"]]
        print(f"{n:>18} | " + " | ".join(row))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)

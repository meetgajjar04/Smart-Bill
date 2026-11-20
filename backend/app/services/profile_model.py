"""Local, offline 'home-profile' model for electricity.

1. `components()` is a transparent physics-style load model (kWh per month by appliance group, driven by
   climate zone and month) built from typical Indian household figures.
2. A HistGradientBoostingRegressor is trained on a synthetic population of ~6,000 households x 12 months
   generated from (1) plus household-level behaviour noise. This is the 'prior': it can forecast from day one.
3. analysis/ml.py then calibrates the prior to the user's real bills (see ml.ensemble_forecast).

The prior is only as good as these assumptions — which is why it is always blended with the user's own data."""
import logging
import threading
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import r2_score

from ..config import settings

log = logging.getLogger("smartbill.profile_model")
MODEL_VERSION = "v1"

# Monthly demand indices (Jan..Dec) per climate zone: 0 = none, 1 = full load.
COOL = {
    "hot_humid": [.45, .50, .65, .80, .90, .70, .60, .60, .65, .70, .55, .45],
    "hot_dry":   [.05, .15, .40, .80, 1.0, 1.0, .85, .80, .75, .50, .20, .05],
    "composite": [.05, .10, .35, .70, .95, .95, .80, .75, .70, .45, .15, .05],
    "temperate": [.05, .20, .50, .70, .60, .25, .15, .15, .20, .30, .10, .05],
    "cold":      [0, 0, .05, .15, .30, .30, .20, .20, .10, .05, 0, 0],
}
HEAT = {
    "hot_humid": [.30, .20, .05, 0, 0, 0, 0, 0, 0, 0, .10, .25],
    "hot_dry":   [.80, .60, .25, 0, 0, 0, 0, 0, 0, .10, .45, .80],
    "composite": [.80, .60, .25, 0, 0, 0, 0, 0, 0, .10, .45, .80],
    "temperate": [.50, .35, .10, 0, 0, 0, 0, 0, 0, 0, .20, .45],
    "cold":      [1.0, .90, .70, .40, .15, 0, 0, 0, .10, .40, .80, 1.0],
}
SOLAR = [.90, .95, 1.0, 1.0, .95, .70, .55, .55, .70, .90, .90, .85]  # monsoon dip
CITY_ZONE = {
    "mumbai": "hot_humid", "navi mumbai": "hot_humid", "thane": "hot_humid", "chennai": "hot_humid", "kolkata": "hot_humid",
    "kochi": "hot_humid", "goa": "hot_humid", "visakhapatnam": "hot_humid", "mangalore": "hot_humid", "surat": "hot_humid",
    "delhi": "composite", "new delhi": "composite", "noida": "composite", "gurgaon": "composite", "gurugram": "composite",
    "lucknow": "composite", "hyderabad": "composite", "chandigarh": "composite", "patna": "composite", "bhopal": "composite",
    "jaipur": "hot_dry", "ahmedabad": "hot_dry", "nagpur": "hot_dry", "jodhpur": "hot_dry", "indore": "hot_dry", "raipur": "hot_dry",
    "bengaluru": "temperate", "bangalore": "temperate", "pune": "temperate", "mysuru": "temperate", "nashik": "temperate",
    "coimbatore": "temperate", "shimla": "cold", "dehradun": "cold", "srinagar": "cold", "manali": "cold",
}
DEFAULTS = dict(area_sqft=800, occupants=3, ac_count=1, ac_hours_peak=6, fridges=1, electric_geyser=0, induction=0,
                washing_machine=1, water_pump=0, ev_km_per_day=0, solar_kw=0, wfh_days=1)
PROFILE_FIELDS = ["city", "climate_zone", *DEFAULTS.keys(), "tariff_per_unit", "fixed_charge"]

# The questions we ask, most influential first. `weight` = share of forecast quality it explains (sums to 100).
QUESTIONS = [
    dict(id="city", group="Your home", label="Which city do you live in?", type="text", weight=15,
         why="Your climate decides how much you cool and heat — the biggest seasonal swing in a bill."),
    dict(id="ac_count", group="Cooling & heating", label="How many air conditioners do you run?", type="number", min=0, max=10, weight=15,
         why="ACs are usually the largest single electricity load."),
    dict(id="ac_hours_peak", group="Cooling & heating", label="Hours per day each AC runs in peak summer", type="number", min=0, max=24, weight=15,
         why="Hours × number of ACs sets your summer peak.", depends_on="ac_count"),
    dict(id="area_sqft", group="Your home", label="Home size (sq ft)", type="number", min=100, max=10000, weight=10,
         why="Bigger homes have more lighting, fans and cooling volume."),
    dict(id="occupants", group="Your home", label="How many people live there?", type="number", min=1, max=20, weight=10,
         why="More people means more hot water, laundry and appliance use."),
    dict(id="electric_geyser", group="Cooling & heating", label="Do you use an electric water heater (geyser)?", type="bool", weight=8,
         why="A geyser adds a winter bump to your bill."),
    dict(id="ev_km_per_day", group="Appliances", label="Electric vehicle: km driven per day (0 if none)", type="number", min=0, max=400, weight=5,
         why="Charging an EV at home can add 100+ units a month."),
    dict(id="solar_kw", group="Appliances", label="Rooftop solar size in kW (0 if none)", type="number", min=0, max=50, weight=5,
         why="Solar cuts the units you are billed for, mostly outside monsoon."),
    dict(id="induction", group="Appliances", label="Do you cook on an induction stove?", type="bool", weight=4,
         why="Induction cooking adds roughly 30–40 units a month."),
    dict(id="wfh_days", group="Your home", label="Days per week someone works from home", type="number", min=0, max=7, weight=4,
         why="Home working adds daytime fan, AC and laptop use."),
    dict(id="washing_machine", group="Appliances", label="Do you have a washing machine?", type="bool", weight=3, why="Small but steady load."),
    dict(id="water_pump", group="Appliances", label="Do you run an electric water pump?", type="bool", weight=3, why="Pumps can run for hours daily."),
    dict(id="fridges", group="Appliances", label="Number of refrigerators", type="number", min=0, max=4, weight=3, why="Always-on load."),
    dict(id="tariff_per_unit", group="Your tariff (optional)", label="Your price per unit in ₹ (leave empty to use a typical slab tariff)", type="number", min=1, max=40, weight=0,
         why="If you know it, forecasts convert units to rupees more precisely."),
]


def resolve_zone(city: str | None, zone: str | None) -> str:
    if zone in COOL:
        return zone
    return CITY_ZONE.get((city or "").lower().strip(), "composite")


def _merge(p: dict | None) -> dict:
    return {**DEFAULTS, **{k: (int(v) if isinstance(v, bool) else v) for k, v in (p or {}).items() if k in DEFAULTS and v is not None}}


def completeness(p: dict | None) -> int:
    p = p or {}
    got = 0
    for q in QUESTIONS:
        if not q["weight"]:
            continue
        if p.get(q["id"]) is not None or (q.get("depends_on") and not p.get(q["depends_on"]) and p.get(q["depends_on"]) is not None):
            got += q["weight"]
    return got


def unanswered(p: dict | None) -> list[dict]:
    p = p or {}
    out = []
    for q in QUESTIONS:
        if p.get(q["id"]) is None and not (q.get("depends_on") and p.get(q["depends_on"]) == 0):
            out.append(q)
    return sorted(out, key=lambda q: -q["weight"])


def components(p: dict | None, month: int) -> dict[str, float]:
    """kWh per month by appliance group (before solar). Transparent so we can explain the forecast."""
    q = _merge(p)
    zone = resolve_zone((p or {}).get("city"), (p or {}).get("climate_zone"))
    cool, heat = COOL[zone][month - 1], HEAT[zone][month - 1]
    return {
        "Lights & electronics": 15 + 0.015 * q["area_sqft"] + 5 * q["occupants"],
        "Fans & coolers": (12 + 0.012 * q["area_sqft"]) * (0.6 + 0.8 * cool),
        "Refrigerator": 45 * q["fridges"],
        "Air conditioning": q["ac_count"] * q["ac_hours_peak"] * 30 * cool,
        "Water heating": 30 * heat * (0.6 + 0.35 * q["occupants"]) * q["electric_geyser"],
        "Induction cooking": (18 + 5 * q["occupants"]) * q["induction"],
        "Washing machine": (8 + 2 * q["occupants"]) * q["washing_machine"],
        "Water pump": 22 * q["water_pump"],
        "Working from home": q["wfh_days"] * 4.3 * 1.2,
        "EV charging": q["ev_km_per_day"] * 30 * 0.12,
    }


def solar_kwh(p: dict | None, month: int) -> float:
    return _merge(p)["solar_kw"] * 30 * 4.2 * SOLAR[month - 1] * 0.8


def simulate_kwh(p: dict | None, month: int) -> float:
    return max(sum(components(p, month).values()) - solar_kwh(p, month), 0.0)


SLABS = [(100, 5.5), (200, 9.0), (200, 12.5), (float("inf"), 14.5)]  # generic urban slab tariff, ₹/unit
DEFAULT_FIXED = 130.0


def amount_from_kwh(kwh: float, tariff_per_unit: float | None = None, fixed: float | None = None) -> float:
    if tariff_per_unit:
        return (fixed or 0.0) + tariff_per_unit * kwh
    left, total = kwh, 0.0
    for size, rate in SLABS:
        use = min(left, size)
        total += use * rate
        left -= use
        if left <= 0:
            break
    return DEFAULT_FIXED + total


# ------------------------------------------------------------------ the ML model
def _features(p: dict | None, month: int) -> list[float]:
    q = _merge(p)
    zone = resolve_zone((p or {}).get("city"), (p or {}).get("climate_zone"))
    return [q["area_sqft"], q["occupants"], q["ac_count"], q["ac_hours_peak"], q["fridges"], q["electric_geyser"], q["induction"],
            q["washing_machine"], q["water_pump"], q["ev_km_per_day"], q["solar_kw"], q["wfh_days"],
            COOL[zone][month - 1], HEAT[zone][month - 1], SOLAR[month - 1],
            np.sin(2 * np.pi * month / 12), np.cos(2 * np.pi * month / 12)]


def synthetic_population(n: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X, y = [], []
    zones = list(COOL)
    for _ in range(n):
        ac = int(rng.choice([0, 1, 2, 3, 4], p=[.18, .38, .28, .12, .04]))
        p = dict(
            area_sqft=int(np.clip(rng.lognormal(6.8, .4), 350, 4500)), occupants=int(rng.integers(1, 8)), ac_count=ac,
            ac_hours_peak=float(rng.integers(2, 13)) if ac else 0.0, fridges=int(rng.choice([1, 2], p=[.85, .15])),
            electric_geyser=int(rng.random() < .45), induction=int(rng.random() < .2), washing_machine=int(rng.random() < .7),
            water_pump=int(rng.random() < .25), ev_km_per_day=0.0 if rng.random() < .92 else float(rng.uniform(10, 60)),
            solar_kw=0.0 if rng.random() < .93 else float(rng.integers(1, 6)), wfh_days=int(rng.integers(0, 6)),
            climate_zone=str(rng.choice(zones)),
        )
        behaviour = rng.lognormal(0, .15)  # household habits we can't observe — why personal calibration is needed
        for m in range(1, 13):
            X.append(_features(p, m))
            y.append(simulate_kwh(p, m) * behaviour * rng.lognormal(0, .07))
    return np.array(X), np.array(y)


def train(n_households: int = 6000, seed: int = 0) -> dict:
    X, y = synthetic_population(n_households, seed)
    cut = int(len(X) * .85)
    model = HistGradientBoostingRegressor(max_iter=400, learning_rate=.07, max_leaf_nodes=31, l2_regularization=.1, random_state=seed)
    model.fit(X[:cut], y[:cut])
    pred = model.predict(X[cut:])
    mask = y[cut:] > 30
    metrics = {"r2_holdout": round(float(r2_score(y[cut:], pred)), 4),
               "mape_holdout_pct": round(float(np.mean(np.abs(pred[mask] - y[cut:][mask]) / y[cut:][mask]) * 100), 1),
               "households": n_households, "version": MODEL_VERSION}
    model.fit(X, y)  # refit on everything for deployment
    return {"model": model, "metrics": metrics}


_lock, _bundle = threading.Lock(), None


def _path() -> Path:
    return Path(settings.model_dir) / f"prior_{MODEL_VERSION}.joblib"


def load_prior() -> dict:
    global _bundle
    with _lock:
        if _bundle is None:
            path = _path()
            if path.exists():
                _bundle = joblib.load(path)
            else:  # first run without a pre-trained file: train a smaller model once and cache it
                log.warning("No pre-trained prior found; training one (~10 s). Run `python -m scripts.train_prior` to pre-build it.")
                _bundle = train(2500)
                path.parent.mkdir(parents=True, exist_ok=True)
                joblib.dump(_bundle, path)
        return _bundle


def kwh_by_month(p: dict | None) -> dict[int, float]:
    X = np.array([_features(p, m) for m in range(1, 13)])
    return {m: max(float(v), 0.0) for m, v in zip(range(1, 13), load_prior()["model"].predict(X))}


def kwh_from_amount(amount: float, tariff_per_unit: float | None = None, fixed: float | None = None) -> float:
    """Inverse of amount_from_kwh (it is monotonic), so we can work in physical units even from ₹-only bills."""
    if tariff_per_unit:
        return max((amount - (fixed or 0.0)) / tariff_per_unit, 0.0)
    left, kwh = amount - DEFAULT_FIXED, 0.0
    if left <= 0:
        return 0.0
    for size, rate in SLABS:
        cost = size * rate
        if left <= cost:
            return kwh + left / rate
        left -= cost
        kwh += size
    return kwh


class Prior:
    """Profile-model expectations for one household. Calibration happens in kWh (so slab tariffs are respected)."""

    def __init__(self, p: dict | None):
        self._kwh = kwh_by_month(p)
        self.rate, self.fixed = (p or {}).get("tariff_per_unit"), (p or {}).get("fixed_charge")

    def kwh(self, month: int) -> float:
        return self._kwh[month]

    def to_amount(self, kwh: float) -> float:
        return amount_from_kwh(kwh, self.rate, self.fixed)

    def to_kwh(self, amount: float) -> float:
        return kwh_from_amount(amount, self.rate, self.fixed)

    def __call__(self, month: int) -> float:  # uncalibrated expected ₹
        return self.to_amount(self._kwh[month])


def make_prior(p: dict | None) -> Prior:
    return Prior(p)


def drivers(p: dict | None, month: int) -> list[dict]:
    comps = {k: v for k, v in components(p, month).items() if v > 0.5}
    total = sum(comps.values()) or 1
    out = [{"name": k, "kwh": round(v), "share": round(v / total * 100)} for k, v in comps.items()]
    sol = solar_kwh(p, month)
    if sol > 0.5:
        out.append({"name": "Solar offset", "kwh": -round(sol), "share": -round(sol / total * 100)})
    return sorted(out, key=lambda d: -d["kwh"])

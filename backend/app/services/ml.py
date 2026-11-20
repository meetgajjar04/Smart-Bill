"""Forecasting + anomaly detection. Pure functions over plain lists so they are easy to unit-test."""
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import Ridge


def _design(t: np.ndarray, months: list[int] | None) -> np.ndarray:
    cols = [t]
    if months is not None:  # yearly seasonality as a smooth sin/cos pair
        m = np.array(months, dtype=float)
        cols += [np.sin(2 * np.pi * m / 12), np.cos(2 * np.pi * m / 12)]
    return np.column_stack(cols)


def forecast_next(values: list[float], months: list[int] | None = None, next_month: int | None = None) -> dict | None:
    """Predict next period. Method adapts to history length:
       n<3 mean | 3<=n<12 Ridge trend | n>=12 Ridge trend + seasonality."""
    y = np.asarray(values, dtype=float)
    n = len(y)
    if n == 0:
        return None
    if n < 3:
        pred, std, method = float(y.mean()), float(y.std() or 0.1 * y.mean()), "average (need 3+ months for a trend)"
    else:
        seasonal = n >= 12 and months is not None and next_month is not None
        t = np.arange(n, dtype=float)
        X = _design(t, months if seasonal else None)
        model = Ridge(alpha=1.0).fit(X, y)
        resid = y - model.predict(X)
        std = float(np.sqrt((resid ** 2).sum() / max(n - X.shape[1] - 1, 1)))
        Xn = _design(np.array([float(n)]), [next_month] if seasonal else None)
        pred = float(model.predict(Xn)[0])
        pred = float(np.clip(pred, 0.5 * y.min(), 1.6 * y.max()))  # guard against runaway extrapolation
        method = "trend + seasonality (Ridge)" if seasonal else "linear trend (Ridge)"
    return {
        "amount": round(max(pred, 0), 2),
        "low": round(max(pred - 1.96 * std, 0), 2),
        "high": round(pred + 1.96 * std, 2),
        "method": method,
        "n_months": n,
    }


def backtest(values: list[float], months: list[int] | None = None, min_train: int = 3) -> list[dict]:
    """Walk-forward one-step-ahead predictions — gives honest 'predicted vs actual'."""
    out = []
    for i in range(min_train, len(values)):
        f = forecast_next(values[:i], months[:i] if months else None, months[i] if months else None)
        out.append({"index": i, "actual": round(values[i], 2), "predicted": f["amount"]})
    return out


def mape(rows: list[dict]) -> float | None:
    rows = [r for r in rows if r["actual"] > 0]
    if not rows:
        return None
    return round(float(np.mean([abs(r["actual"] - r["predicted"]) / r["actual"] for r in rows]) * 100), 1)


def detect_anomaly(history: list[float], latest: float, seasonal_ref: float | None = None,
                   expected_high: float | None = None) -> dict:
    """Flags unusual *increases*. Robust z-score (median/MAD) + % jump, corroborated by Isolation Forest
       once there are 12+ points. Robust stats keep one earlier spike from masking the next one."""
    h = np.asarray(history, dtype=float)
    base = {"is_anomaly": False, "severity": "none", "z_score": 0.0, "pct_vs_avg": 0.0, "pct_vs_prev": 0.0,
            "avg": None, "reason": "Not enough history yet (need 3+ months).", "method": "insufficient-data"}
    if len(h) < 3:
        return base
    mean, med = float(h.mean()), float(np.median(h))
    mad = float(np.median(np.abs(h - med)))
    scale = 1.4826 * mad if mad > 0 else max(float(h.std()), 0.05 * mean, 1e-6)
    z = (latest - med) / scale
    pct_avg = (latest - mean) / mean * 100 if mean else 0.0
    pct_prev = (latest - h[-1]) / h[-1] * 100 if h[-1] else 0.0

    if_flag = False
    if len(h) >= 12:
        iso = IsolationForest(n_estimators=200, contamination="auto", random_state=42).fit(h.reshape(-1, 1))
        if_flag = bool(iso.predict([[latest]])[0] == -1 and latest > med)

    flagged = z >= 3 or (z >= 2 and pct_avg >= 20) or pct_avg >= 50 or (if_flag and pct_avg >= 15)
    flagged = flagged and pct_avg >= 15  # materiality floor: a +4% bump is never worth an alert
    if flagged and expected_high is not None and latest <= expected_high:  # inside the profile model's 90% range
        return {**base, "reason": "Higher than your average, but in line with what your home profile predicts for this month.",
                "z_score": round(float(z), 2), "pct_vs_avg": round(pct_avg, 1), "pct_vs_prev": round(pct_prev, 1),
                "avg": round(mean, 2), "method": "profile-expected range"}
    seasonal_ok = seasonal_ref is not None and latest <= seasonal_ref * 1.15
    if flagged and seasonal_ok:  # same month last year was just as high -> expected seasonality
        return {**base, "reason": f"Higher than average, but in line with the same month last year (₹{seasonal_ref:,.0f}).",
                "z_score": round(float(z), 2), "pct_vs_avg": round(pct_avg, 1), "pct_vs_prev": round(pct_prev, 1),
                "avg": round(mean, 2), "method": "seasonal check"}
    severity = "none"
    if flagged:
        severity = "high" if (z >= 4 or pct_avg >= 50) else "medium" if (z >= 3 or pct_avg >= 30) else "low"
    reason = (f"{pct_avg:+.0f}% vs your {len(h)}-month average (₹{mean:,.0f}), {pct_prev:+.0f}% vs last month."
              if flagged else "Within your normal range.")
    return {"is_anomaly": flagged, "severity": severity, "z_score": round(float(z), 2), "pct_vs_avg": round(pct_avg, 1),
            "pct_vs_prev": round(pct_prev, 1), "avg": round(mean, 2), "reason": reason,
            "method": "robust z-score + IsolationForest" if len(h) >= 12 else "robust z-score"}


# =====================================================================================
# Hybrid ensemble: home-profile model (calibrated to the user) + trend + recent + seasonal.
# Weights come from *walk-forward backtest error*, so whichever model has actually been
# right for THIS household gets trusted more as history grows.
# =====================================================================================
from typing import Callable  # noqa: E402

_DEFAULT_ERR = {"profile": 0.25, "recent": 0.30, "trend": 0.28, "seasonal": 0.22}
LABELS = {"profile": "Home-profile model (calibrated to your bills)", "recent": "Recent level (season-adjusted)",
          "trend": "Trend (season-adjusted)", "seasonal": "Same month last year (+ growth)"}


def _kwh(prior, month):  # Prior objects expose kWh/₹ conversion; plain callables are treated as ₹-linear
    return prior.kwh(month) if hasattr(prior, "kwh") else prior(month)


def _to_amount(prior, x):
    return prior.to_amount(x) if hasattr(prior, "to_amount") else x


def _to_kwh(prior, a):
    return prior.to_kwh(a) if hasattr(prior, "to_kwh") else a


def calibration_factor(values: list[float], ords: list[int], prior, m: float = 2.0) -> float:
    """How much higher/lower this household runs vs the profile model, measured in physical units (kWh)
       so slab tariffs don't distort it. Shrunk toward 1.0 when data is scarce."""
    ratios = []
    for v, o in list(zip(values, ords))[-8:]:
        p = _kwh(prior, o % 12 + 1)
        if p and p > 0 and v > 0:
            ratios.append(_to_kwh(prior, v) / p)
    if not ratios:
        return 1.0
    n = len(ratios)
    return float(np.clip((n * np.median(ratios) + m) / (n + m), 0.4, 2.5))


def _candidates(vals: list[float], ords: list[int], next_ord: int, prior) -> dict[str, float]:
    out: dict[str, float] = {}
    n = len(vals)
    nxt_m = next_ord % 12 + 1
    ratios = []  # household multiplier vs the profile model, per month: separates seasonality from drift
    if prior is not None:
        ratios = [_to_kwh(prior, v) / _kwh(prior, o % 12 + 1) for v, o in zip(vals, ords) if _kwh(prior, o % 12 + 1) > 0]
    seasonal_prior = prior is not None and _kwh(prior, nxt_m) > 0 and len(ratios) == n and n >= 1

    if seasonal_prior:  # deseasonalised: forecast the multiplier, then re-apply next month's seasonal shape
        base = _kwh(prior, nxt_m)
        out["recent"] = _to_amount(prior, base * float(np.mean(ratios[-3:])))
        if n >= 3:
            r = forecast_next([x * 100 for x in ratios])["amount"] / 100
            out["trend"] = _to_amount(prior, base * max(r, 0.2))
        out["profile"] = _to_amount(prior, base * calibration_factor(vals, ords, prior))
    elif n >= 1:
        out["recent"] = float(np.mean(vals[-3:]))
        if n >= 3:
            out["trend"] = forecast_next(list(vals))["amount"]
        if prior is not None and _kwh(prior, nxt_m) > 0:
            out["profile"] = _to_amount(prior, _kwh(prior, nxt_m))
    elif prior is not None and _kwh(prior, nxt_m) > 0:
        out["profile"] = _to_amount(prior, _kwh(prior, nxt_m))

    by_ord = dict(zip(ords, vals))
    if (next_ord - 12) in by_ord:
        pairs = [(v, by_ord[o - 12]) for v, o in list(zip(vals, ords))[-3:] if (o - 12) in by_ord and by_ord[o - 12] > 0]
        growth = float(np.clip(np.median([a / b for a, b in pairs]), 0.7, 1.5)) if pairs else 1.0
        out["seasonal"] = by_ord[next_ord - 12] * growth
    return out


def _weights(errs: dict[str, list[float]], keys, prior_err: float) -> dict[str, float]:
    raw = {}
    for k in keys:
        e = float(np.mean(errs[k])) if errs.get(k) else (prior_err if k == "profile" else _DEFAULT_ERR[k])
        if errs.get(k) and len(errs[k]) < 3:  # few observations: blend observed error with the default
            e = 0.5 * e + 0.5 * (prior_err if k == "profile" else _DEFAULT_ERR[k])
        raw[k] = 1.0 / (e + 0.03) ** 2
    s = sum(raw.values())
    return {k: v / s for k, v in raw.items()}


def ensemble_forecast(values: list[float], ords: list[int], next_ord: int, prior=None, prior_rel_err: float = 0.25) -> dict | None:
    n = len(values)
    if n == 0 and prior is None:
        return None
    errs: dict[str, list[float]] = {}
    ens_err, backtest_rows = [], []
    for i in range(1 if prior else 2, n):
        c = _candidates(values[:i], ords[:i], ords[i], prior)
        w = _weights(errs, c.keys(), prior_rel_err)
        pred = sum(w[k] * c[k] for k in c)
        actual = values[i]
        ens_err.append(abs(actual - pred) / max(actual, 1.0))
        backtest_rows.append({"ord": ords[i], "actual": round(actual, 2), "predicted": round(pred, 2)})
        for k, f in c.items():  # update per-model error history AFTER predicting (no leakage)
            errs.setdefault(k, []).append(abs(actual - f) / max(actual, 1.0))

    cands = _candidates(values, ords, next_ord, prior)
    w = _weights(errs, cands.keys(), prior_rel_err)
    f = max(sum(w[k] * cands[k] for k in cands), 0.0)
    if len(ens_err) >= 3:
        sigma = max(0.05, float(np.sqrt(np.mean(np.square(ens_err)))))
    else:
        sigma = prior_rel_err if prior else 0.25
    models = [{"key": k, "name": LABELS[k], "forecast": round(cands[k], 2), "weight": round(w[k], 3),
               "backtest_mape": round(float(np.mean(errs[k])) * 100, 1) if errs.get(k) else None} for k in cands]
    return {"amount": round(f, 2), "low": round(max(f * (1 - 1.645 * sigma), 0), 2), "high": round(f * (1 + 1.645 * sigma), 2),
            "method": "hybrid ensemble" if len(cands) > 1 else next(iter(models))["name"], "n_months": n,
            "rel_sigma": round(sigma, 3), "models": sorted(models, key=lambda m: -m["weight"]),
            "mape": round(float(np.mean(ens_err)) * 100, 1) if ens_err else None, "backtest": backtest_rows}

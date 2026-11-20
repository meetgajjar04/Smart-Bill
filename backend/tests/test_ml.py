from app.services import ml
from app.services.recommend import recommend

SIX = [1200, 1300, 1250, 1400, 1350, 1500]


def test_forecast_follows_trend():
    f = ml.forecast_next(SIX)
    assert 1400 < f["amount"] < 1700 and f["low"] <= f["amount"] <= f["high"]


def test_forecast_small_data_and_empty():
    assert ml.forecast_next([]) is None
    assert ml.forecast_next([1000, 1100])["amount"] == 1050


def test_seasonal_forecast_with_12_plus_points():
    vals = [1000 + 300 * (1 if m in (4, 5, 6) else 0) for m in [7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8]]
    months = [7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8]
    assert "seasonality" in ml.forecast_next(vals, months, 9)["method"]


def test_anomaly_detected_and_normal_ignored():
    assert ml.detect_anomaly(SIX, 2900)["is_anomaly"] is True
    assert ml.detect_anomaly(SIX, 1450)["is_anomaly"] is False
    assert ml.detect_anomaly([1000, 1000, 1000], 1000)["is_anomaly"] is False
    assert ml.detect_anomaly([1000, 1000], 5000)["method"] == "insufficient-data"


def test_backtest_and_mape():
    bt = ml.backtest(SIX)
    assert len(bt) == 3 and ml.mape(bt) < 15


def test_recommendations_use_history():
    series = {"electricity": [1000, 1000, 1000, 1300, 1400, 1500], "mobile": [499] * 6}
    r = recommend(series, late_count=2, anomalies={}, next_month=4)
    titles = " ".join(x["title"] for x in r)
    assert "trending up" in titles and "late" in titles and r[0]["priority"] == "high"


def test_small_bump_not_flagged_even_with_stable_history():
    assert ml.detect_anomaly([280, 280, 281, 279, 280], 291)["is_anomaly"] is False


def test_seasonal_peak_is_not_an_anomaly():
    hist = [1200, 1250, 1300, 1280, 1220, 1260]
    assert ml.detect_anomaly(hist, 2000)["is_anomaly"] is True
    assert ml.detect_anomaly(hist, 2000, seasonal_ref=1950)["is_anomaly"] is False

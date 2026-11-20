"""Personalised, explainable cost-saving advice from the user's own history."""
from statistics import mean

_PRIORITY = {"high": 0, "medium": 1, "low": 2}


def _trend(vals: list[float]) -> float | None:
    if len(vals) < 6:
        return None
    return mean(vals[-3:]) / mean(vals[-6:-3]) - 1 if mean(vals[-6:-3]) else None


def recommend(series: dict[str, list[float]], late_count: int, anomalies: dict[str, dict], next_month: int) -> list[dict]:
    recs: list[dict] = []
    avgs = {c: mean(v[-6:]) for c, v in series.items() if v}
    total = sum(avgs.values()) or 1

    def add(cat, title, detail, pct, priority):
        saving = round(avgs.get(cat, 0) * pct / 100) if cat in avgs else None
        recs.append({"category": cat, "title": title, "detail": detail, "est_monthly_saving": saving, "priority": priority})

    for cat, avg in avgs.items():
        share = avg / total * 100
        tr = _trend(series[cat])
        an = anomalies.get(cat)

        if share >= 45 and len(avgs) > 1:
            add(cat, f"{cat.title()} is {share:.0f}% of your bills",
                "This is your biggest expense, so even small cuts here matter most.", 0, "medium")

        if an and an.get("is_anomaly"):
            add(cat, f"Investigate your latest {cat} bill",
                f"It's unusually high ({an['reason']}). Check for a billing error, a plan change, or a faulty appliance/leak, and raise a dispute with the provider if it looks wrong.",
                10, "high")

        if cat == "electricity":
            if tr is not None and tr > 0.10:
                add(cat, "Your electricity use is trending up",
                    f"The last 3 months average {tr*100:.0f}% more than the 3 before. Audit always-on loads: old fridge, geyser timer, standby devices.", 8, "high")
            if next_month in (3, 4, 5, 6):
                add(cat, "Summer is coming — cool smarter",
                    "Set the AC to 24–26 °C (each degree lower costs roughly 6% more), use the fan alongside it, and clean filters monthly.", 10, "medium")
            add(cat, "Switch to LED and 5-star appliances",
                "LEDs use about 80% less power than incandescent bulbs; a 5-star AC/fridge pays back within a few years.", 6, "low")
            add(cat, "Shift heavy loads off-peak",
                "If your tariff has time-of-day rates, run washing machine, geyser and pump at off-peak hours.", 4, "low")

        if cat == "mobile" and avg > 400:
            add(cat, "Compare mobile plans",
                "Your average mobile bill is high. Check if an annual prepaid pack or a family/shared postpaid plan covers your data and calls for less.", 20, "medium")
        if cat == "internet" and avg > 900:
            add(cat, "Right-size your broadband plan",
                "Ask your ISP for a yearly-prepay discount or a lower tier. Most households stream fine on 100–200 Mbps.", 15, "medium")
        if cat == "water" and tr is not None and tr > 0.15:
            add(cat, "Possible water leak",
                "Usage is climbing. Close all taps and see if the meter still moves; fix dripping taps and running cisterns.", 12, "high")
        if cat == "gas" and avg > 0:
            add(cat, "Cook efficiently",
                "Use pressure cookers, keep lids on, and match pan size to the burner to trim gas use.", 8, "low")

    if late_count >= 1:
        add("all", f"You paid {late_count} bill(s) late",
            "Late fees and reconnection charges add up. Turn on auto-pay or keep the n8n deadline reminders enabled.", 0, "high")
    if len(avgs) >= 2 and "mobile" in avgs and "internet" in avgs:
        add("all", "Bundle mobile + broadband",
            "Many providers give 10–20% off when both are on one account — ask for a convergence plan.", 0, "low")

    recs.sort(key=lambda r: (_PRIORITY[r["priority"]], -(r["est_monthly_saving"] or 0)))
    return recs[:12]

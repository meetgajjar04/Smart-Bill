"""Demo data: python -m scripts.seed [--allow-live] [--reset --yes]
Creates demo@smartbill.ai / Demo@12345 with 18 months of seasonal bills.
`--allow-live` is required for any database not named *_test; `--yes` confirms a reset.
"""
from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

from app.database import Base, SessionLocal, engine, init_db
from app.models import Bill, HouseholdProfile, User
from app.security import hash_password
from app.services import profile_model as pm

random.seed(7)


def month_start(offset: int) -> date:
    t = date.today().replace(day=1)
    y, m = t.year, t.month + offset
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, 1)


def main(reset: bool = False, allow_live: bool = False, yes: bool = False):
    target = engine.url.database or "<unknown>"
    print(f"Target database: {target} ({engine.url.render_as_string(hide_password=True)})")

    if not target.endswith("_test") and not allow_live:
        raise RuntimeError(
            f"Refusing to seed {target!r}: pass --allow-live to seed a real database explicitly."
        )
    if reset and not yes:
        raise RuntimeError("--reset drops every table in the target database. Re-run with --yes to confirm.")

    if reset:
        # Print the target immediately before the destructive action.
        print(f"DESTRUCTIVE: dropping every table in {target!r} before rebuilding the schema...")
        Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as db:
        if db.query(User).filter_by(email="demo@smartbill.ai").first():
            print("Demo user already exists (use --reset to rebuild).")
            return
        u = User(email="demo@smartbill.ai", name="Demo User", password_hash=hash_password("Demo@12345"))
        db.add(u)
        db.flush()
        home = dict(city="Mumbai", area_sqft=900, occupants=3, ac_count=2, ac_hours_peak=6, fridges=1, electric_geyser=True,
                    induction=False, washing_machine=True, water_pump=False, ev_km_per_day=0, solar_kw=0, wfh_days=2)
        db.add(HouseholdProfile(user_id=u.id, **home))
        for off in range(-17, 1):
            bd = month_start(off) + timedelta(days=4)
            dd = bd + timedelta(days=15)
            # this household runs ~12% above the generic profile estimate, with mild growth and noise
            units = round(pm.simulate_kwh(home, bd.month) * 1.12 * (1 + 0.004 * (off + 17)) * random.uniform(0.95, 1.05))
            elec = round(pm.amount_from_kwh(units) * 1.03)
            if off == -1:  # the anomaly the demo should catch
                units, elec = round(units * 1.7), round(elec * 1.85)
            rows = [
                ("electricity", "Adani Electricity", elec, units),
                ("mobile", "Jio", random.choice([399, 399, 399, 449]), None),
                ("internet", "ACT Fibernet", 1049, None),
                ("water", "MCGM", round(280 * random.uniform(0.9, 1.1)), None),
            ]
            for cat, prov, amt, un in rows:
                past = dd < date.today()
                late = past and random.random() < 0.08
                db.add(Bill(user_id=u.id, category=cat, provider=prov, bill_date=bd, due_date=dd, amount=amt, units=un,
                            reviewed=True, paid=past, paid_on=(dd + timedelta(days=3) if late else dd - timedelta(days=2)) if past else None))
        db.commit()
        print("Seeded. Sign in with demo@smartbill.ai / Demo@12345")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true", help="required for a database not ending in _test")
    parser.add_argument("--reset", action="store_true", help="drop all tables first (DESTRUCTIVE)")
    parser.add_argument("--yes", action="store_true", help="confirm --reset")
    args = parser.parse_args()
    main(reset=args.reset, allow_live=args.allow_live, yes=args.yes)

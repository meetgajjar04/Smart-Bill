from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import HouseholdProfile, User
from ..schemas import HomeProfileIn
from ..services import ai_cache, analysis, profile_model as pm

router = APIRouter(prefix="/profile", tags=["home profile"])


def _view(db: Session, user: User) -> dict:
    p = analysis.get_profile(db, user.id) or {}
    zone = pm.resolve_zone(p.get("city"), p.get("climate_zone"))
    known_city = (p.get("city") or "").lower().strip() in pm.CITY_ZONE
    qs = pm.unanswered(p)
    return {
        "profile": p, "completeness": pm.completeness(p), "climate_zone": zone,
        "zone_guessed": bool(p.get("city")) and not known_city and not p.get("climate_zone"),
        "questions": pm.QUESTIONS, "unanswered": [q["id"] for q in qs],
        "model": {k: v for k, v in pm.load_prior()["metrics"].items()},
    }


@router.get("")
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _view(db, user)


@router.put("")
def save_profile(data: HomeProfileIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(HouseholdProfile).filter_by(user_id=user.id).first() or HouseholdProfile(user_id=user.id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    ai_cache.invalidate_portfolio(db, user.id)
    return _view(db, user)

"""Pre-build the local prior model:  python -m scripts.train_prior  (runs at Docker build time)."""
from pathlib import Path

import joblib

from app.config import settings
from app.services import profile_model as pm

if __name__ == "__main__":
    bundle = pm.train(6000)
    path = Path(settings.model_dir) / f"prior_{pm.MODEL_VERSION}.joblib"
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    print("saved", path, bundle["metrics"])

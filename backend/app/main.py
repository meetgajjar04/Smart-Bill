import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import db_healthy, init_db
from .deps import SAFE_METHODS, verify_csrf
from .routers import ai, analytics, auth, bills, internal, notifications, profile
from .services import llm, reminders

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _warm_ai() -> None:
    """Load Smart Bill AI's models in the background so the first question is quick."""
    try:
        await asyncio.to_thread(llm.warm)
    except Exception:  # never let an optional warm-up stop the API from starting
        pass
    if llm.analysis_model() != llm.chat_model():
        try:
            await asyncio.to_thread(llm.warm, True)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.assert_safe()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    init_db()  # MySQL: db/mysql_schema.sql; SQLite: ORM metadata

    tasks = []
    if settings.email_enabled:
        # In-app reminder alerts work without SMTP; the mailer skips sends when unconfigured.
        tasks.append(asyncio.create_task(reminders.reminder_loop()))
    if settings.ollama_enabled and settings.ollama_warmup:
        tasks.append(asyncio.create_task(_warm_ai()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Smart Bill AI", version="1.0.0", lifespan=lifespan,
              docs_url=None if settings.is_prod else "/api/docs",
              redoc_url=None, openapi_url=None if settings.is_prod else "/api/openapi.json")

app.add_middleware(CORSMiddleware, allow_origins=sorted(settings.allowed_origins),
                   allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-Requested-With", "X-CSRF-Token"])


def _secure_headers(resp) -> None:
    resp.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        ),
    })


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Browsers send Origin on state-changing requests. Reject anything except an
    # explicit configured origin; this also protects cookie-less login/register.
    if request.method not in SAFE_METHODS:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin and origin not in settings.allowed_origins:
            resp = JSONResponse({"detail": "Invalid Origin."}, status_code=403)
            _secure_headers(resp)
            return resp
    resp = await call_next(request)
    _secure_headers(resp)
    return resp


for r in (auth.router, bills.router, analytics.router, profile.router, ai.router,
          notifications.router):
    app.include_router(r, prefix="/api", dependencies=[Depends(verify_csrf)])

# n8n authenticates with X-Internal-Key, not a browser cookie, so it must not be
# forced to send browser CSRF headers.
app.include_router(internal.router, prefix="/api")


@app.get("/api/health", tags=["meta"])
@app.get("/health", include_in_schema=False)
def health():
    database_ok = db_healthy()
    payload = {"status": "ok" if database_ok else "unavailable",
               "database": "ok" if database_ok else "unavailable"}
    return JSONResponse(payload, status_code=200 if database_ok else 503)

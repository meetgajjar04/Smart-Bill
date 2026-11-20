import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo keeps one .env at its root. Look there first, then fall back to a
# backend-local .env (and finally to real environment variables, which always win).
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"

# Production-ready handoffs do not ship secrets in the project tree. For this
# machine the rotated values live in a protected file outside the repo. Load it
# into the process environment before Settings is built, but never overwrite a
# value that was supplied explicitly by the shell or a test fixture.
# Override the location with SMARTBILL_SECRETS_FILE on another machine.
_DEFAULT_SECRETS_FILE = Path.home() / ".smartbill" / "handoff-secrets.env"


def _load_external_secrets() -> None:
    configured = os.environ.get("SMARTBILL_SECRETS_FILE", "").strip()
    path = Path(configured).expanduser() if configured else _DEFAULT_SECRETS_FILE
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_external_secrets()

_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "dev-", "replace-me", "your-", "example-")


def _is_placeholder(value: str) -> bool:
    """True for the sort of value shipped in .env.example, not a real secret."""
    lowered = value.strip().lower()
    return (not lowered or lowered.startswith(_PLACEHOLDER_PREFIXES)
            or "placeholder" in lowered or "at-least-32" in lowered)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(str(_ROOT_ENV), ".env"), extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./smartbill.db"
    secret_key: str = "change-me-in-production-please-use-32-chars-min"
    access_token_minutes: int = 15
    # JWTs carry an issuer/audience and a key id so old keys can be retired
    # without invalidating every session at once.
    jwt_issuer: str = "smartbill-ai"
    jwt_audience: str = "smartbill-web"
    jwt_key_id: str = "primary"
    jwt_previous_secret_keys: str = ""  # comma-separated; oldest first is fine
    # Keyed HMAC for rate-limit buckets. Keep it different from the JWT keys so
    # rotating sessions does not silently re-bucket every client.
    rate_limit_secret: str = "change-me-rate-limit-secret-at-least-32-characters"
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    # Set true only when a trusted reverse proxy overwrites X-Forwarded-For.
    # Number of trusted hops nearest the app (proxy itself plus any upstream
    # proxies); XFF is read from the RIGHT by this many entries.
    trust_proxy_headers: bool = False
    trusted_proxy_hops: int = 1
    upload_dir: str = "./uploads"
    max_upload_mb: int = 15
    max_drafts_per_user: int = 20
    # n8n integration
    internal_api_key: str = "dev-internal-key"
    n8n_webhook_url: str = ""  # e.g. http://n8n:5678/webhook/bill-analysis
    n8n_webhook_secret: str = "dev-webhook-secret"
    n8n_encryption_key: str = ""
    tesseract_cmd: str = ""
    tessdata_prefix: str = ""  # folder holding *.traineddata (needed by portable Tesseract builds)
    poppler_path: str = ""     # folder holding pdftoppm(.exe), needed by pdf2image on Windows
    ocr_lang: str = "eng"  # e.g. "eng+hin" if the extra Tesseract language data is installed
    model_dir: str = "./model_store"
    # Smart Bill AI through Ollama (free, runs on your machine). Falls back to the rule parser when absent.
    ollama_url: str = "http://127.0.0.1:11434"  # numeric IP avoids a slow localhost/IPv6 lookup
    # Leave blank to use the first model installed in Ollama. Set a name only if you
    # keep several models and want to choose between them. ``ollama_model`` writes
    # chat replies; ``ollama_analysis_model`` runs bill/portfolio deep analysis.
    # Leave the analysis model blank to reuse the chat model.
    ollama_model: str = ""
    ollama_analysis_model: str = ""
    ollama_timeout: float = 90.0  # seconds per generation; small models on CPU need patience
    ollama_enabled: bool = True
    ollama_extract: bool = True  # also use the LLM to read/validate uploaded bills
    # Speed tuning. One fixed context/thread count keeps the model resident: Ollama
    # reloads the weights (several seconds) whenever these change between requests.
    ollama_num_ctx: int = 4096
    ollama_num_thread: int = 0  # 0 = auto (half the logical CPUs, best on laptops)
    ollama_keep_alive: str = "30m"  # keep the model warm between questions
    ollama_warmup: bool = True  # load the model in the background when the backend starts

    # E-mail delivery (deadline reminders + unusual-bill alerts) through any SMTP account.
    email_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False      # implicit TLS, usually port 465 (takes precedence over STARTTLS)
    reminder_days_before: int = 1  # send the deadline e-mail this many days before the due date
    app_url: str = "http://localhost:5173"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.email_enabled and (self.smtp_from or self.smtp_username))

    @property
    def allowed_origins(self) -> set[str]:
        """Explicit browser origins accepted on state-changing requests."""
        origins = {o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()}
        if self.app_url:
            origins.add(self.app_url.strip().rstrip("/"))
        # Local users commonly switch between localhost and 127.0.0.1; treat
        # those as the same configured origin for the browser Origin check.
        for origin in list(origins):
            if "://localhost:" in origin:
                origins.add(origin.replace("://localhost:", "://127.0.0.1:", 1))
            elif "://127.0.0.1:" in origin:
                origins.add(origin.replace("://127.0.0.1:", "://localhost:", 1))
        return origins

    @property
    def jwt_previous_keys(self) -> list[str]:
        return [key.strip() for key in self.jwt_previous_secret_keys.split(",") if key.strip()]

    @property
    def jwt_keys(self) -> dict[str, str]:
        """Signing keys by ``kid``. The current key is always ``JWT_KEY_ID``."""
        keys = {self.jwt_key_id: self.secret_key}
        for index, key in enumerate(self.jwt_previous_keys):
            keys[f"previous-{index + 1}"] = key
        return keys

    def assert_safe(self) -> None:
        """Fail fast on configuration that would make an install unsafe."""
        problems: list[str] = []
        # HS256 must never run with a shipped default, even for a local demo.
        if _is_placeholder(self.secret_key) or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be a unique random value of at least 32 characters")
        # Rotation keys are still signing keys: a weak one would let an attacker
        # mint a token with its old kid and bypass revocation.
        for key_id, key in self.jwt_keys.items():
            if _is_placeholder(key) or len(key) < 32:
                problems.append(f"JWT signing key {key_id!r} must be random and at least 32 characters")
        if _is_placeholder(self.rate_limit_secret) or len(self.rate_limit_secret) < 32:
            problems.append("RATE_LIMIT_SECRET must be a unique random value of at least 32 characters")
        if not self.jwt_key_id:
            problems.append("JWT_KEY_ID must not be empty")
        if len(self.jwt_keys) != len(self.jwt_previous_keys) + 1:
            problems.append("JWT_KEY_ID must not collide with a reserved rotation id (previous-N)")
        if not self.jwt_issuer.strip() or not self.jwt_audience.strip():
            problems.append("JWT_ISSUER and JWT_AUDIENCE must not be empty")
        if self.trusted_proxy_hops < 0:
            problems.append("TRUSTED_PROXY_HOPS must be zero or greater")

        if not self.is_prod:
            if problems:
                raise RuntimeError("Refusing to start:\n  - " + "\n  - ".join(problems))
            return

        for key_id, key in self.jwt_keys.items():
            if _is_placeholder(key) or len(key) < 32:
                problems.append(f"JWT signing key {key_id!r} must be random and at least 32 characters")
        if _is_placeholder(self.internal_api_key) or len(self.internal_api_key) < 24:
            problems.append("INTERNAL_API_KEY must be a unique random value of at least 24 characters")
        if _is_placeholder(self.n8n_webhook_secret) or len(self.n8n_webhook_secret) < 24:
            problems.append("N8N_WEBHOOK_SECRET must be a unique random value of at least 24 characters")
        if self.n8n_encryption_key and (_is_placeholder(self.n8n_encryption_key) or len(self.n8n_encryption_key) < 32):
            problems.append("N8N_ENCRYPTION_KEY must be a random value of at least 32 characters when set")
        all_secrets = [*self.jwt_keys.values(), self.rate_limit_secret,
                       self.internal_api_key, self.n8n_webhook_secret,
                       self.n8n_encryption_key]
        if len(set(all_secrets)) != len(all_secrets):
            problems.append("JWT, RATE_LIMIT, INTERNAL, N8N_WEBHOOK and N8N_ENCRYPTION keys must all differ")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true behind HTTPS")
        if self.database_url.startswith("sqlite"):
            problems.append("production requires MySQL/MariaDB, not the SQLite fallback")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must list explicit origins, never '*'")
        if self.smtp_host and not (self.smtp_ssl or self.smtp_starttls):
            problems.append("SMTP must use STARTTLS or implicit TLS in production")

        if problems:
            raise RuntimeError("Refusing to start in production:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

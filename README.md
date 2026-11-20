# Smart Bill AI

![Local-first](https://img.shields.io/badge/local--first-100%25-brightgreen)
![Backend](https://img.shields.io/badge/backend-FastAPI-009688?logo=fastapi)
![Frontend](https://img.shields.io/badge/frontend-React%20%2B%20Vite-61DAFB?logo=react)
![Database](https://img.shields.io/badge/database-MySQL%20%2F%20MariaDB-4479A1?logo=mysql)
![AI](https://img.shields.io/badge/AI-local%20Ollama-FFD700)
![Tests](https://img.shields.io/badge/tests-166%20passed-success)

**Upload bills. Confirm what was read. Track deadlines. Ask questions. Get numbers you can trust — all on your own machine.**

Smart Bill AI is a private, local-first bill management app for electricity, mobile, internet, water and gas bills. It reads bill images and PDFs with local OCR, lets you correct the extracted data before saving, tracks due dates, analyses spending history, forecasts next month's electricity bill, flags unusual increases and explains ways to spend less.

No paid APIs. No cloud model calls. n8n is optional; the built-in Python pipeline does the work by default.

> **Product branding:** the interface is branded only as **Smart Bill AI**. The local model runtime is an implementation detail, not the product name shown to users.

---

## Contents

| Section | What you'll find |
|---|---|
| [Why Smart Bill AI?](#why-smart-bill-ai) | The problem it solves and the design principles |
| [Features](#features) | What the app can do today |
| [How it works](#how-it-works) | Architecture and request flow |
| [Tech stack](#tech-stack) | Technologies and why they were chosen |
| [Repository map](#repository-map) | Where to find each part of the code |
| [Quick start — Windows](#quick-start--windows-native) | Native setup with XAMPP, Python, Node and Ollama |
| [Quick start — Docker](#quick-start--docker) | Container setup |
| [Configuration](#configuration) | Secrets, environment variables and precedence |
| [Using the app](#using-the-app) | A short tour of the main screens |
| [Verification status](#verification-status) | What was tested and what was not |
| [Security](#security) | Authentication, rate limits, uploads and privacy |
| [Production checklist](#production-checklist) | What to confirm before exposing the app |
| [Troubleshooting](#troubleshooting) | Common startup and runtime issues |
| [Detailed docs](#detailed-docs) | Deep-dive guides |

---

## Why Smart Bill AI?

Most bill-analysis tools send your documents to a cloud service. That is uncomfortable for household financial data, and it makes answers hard to audit. Smart Bill AI was built around four rules:

1. **Your bills stay on your machine.** OCR, parsing, local models, storage and e-mail all run locally or through SMTP you control.
2. **The model writes; Python verifies.** Every figure in a chat answer is calculated from your own bills and checked before it is shown. A model cannot invent a total, percentage or date.
3. **You confirm before anything is saved.** OCR and model extraction produce a draft; the review screen shows the raw text and extracted fields so you can fix mistakes.
4. **The setup is reproducible.** One versioned SQL schema, ordered migrations, launchers for Windows/Unix, and a startup guard that refuses weak secrets.

---

## Features

### 1. Local bill ingestion you can audit

- Upload PDFs, images and phone photos of bills.
- OCR handles EXIF rotation, deskewing, shadows, glare and multiple Tesseract views.
- A local model fills gaps in the rule-based parser; rule-parsed fields always win.
- The review screen shows **What we read (raw OCR text)** and the extracted charges before you save.
- Uploaded files are type-checked by magic bytes, capped at **15 MB**, stored with random names in per-user folders and served only through the authenticated API.

### 2. Deadlines and reminders

- Dashboard shows upcoming and overdue bills.
- The backend runs an hourly reminder loop for reviewed, unpaid bills due in exactly `REMINDER_DAYS_BEFORE` days.
- Successful sends are recorded on `Bill.reminded_at`; the same bill is not reminded again for 20 hours.
- In-app alerts are deduplicated. Unusual-bill e-mail is sent only when a **new** alert is created and the user has opted in with `notify_email`.
- Without SMTP configured, the app still works and alerts stay in-app.

### 3. Ask AI chat with checked answers

- Ask questions about your bills in plain language.
- While generating, the interface shows exactly **`Thinking…`**.
- The portfolio analysis button reads exactly **`Analyze all bills`**.
- Chat remembers the last **12 messages** in the browser (about six exchanges).
- Replies render lightweight markdown: bold, lists, links and compact tables.
- You can upload a bill directly in the chat card; it follows the same read → check → save flow as the Add Bill page.
- Month-table questions are answered directly from the database without asking the model.
- A fact sheet, exact numeric guard, semantic guard, one repair rewrite and sentence dropping keep the answer tied to real data.

### 4. Insights, forecasting and anomaly detection

- Pandas monthly aggregation, category share and stacked charts.
- A hybrid forecast blends a local profile model, a season-adjusted recent level, a season-adjusted trend and same-month-last-year. Weights come from walk-forward backtesting on your own bills.
- A 90% range and the actual past error are shown; forecasts with fewer than six months of data are labelled rough.
- Unusual increases use a robust z-score plus IsolationForest with a 15% materiality floor, suppressed when the bill is inside the calibrated model's expected range.
- Recommendations are explainable rules driven by your own trend, category share, anomalies and late payments — not guarantees.

### 5. Bulk import

- Download a ready-to-fill `.xlsx` template from **Add a bill**.
- Re-upload `.xlsx` or `.csv` (up to 1,000 rows).
- Every row is previewed as ready, duplicate or needing a fix.
- **Nothing is saved until you press Confirm & import.** The confirm request carries a one-time preview token.
- Rejected rows are reported with row numbers and exact problems.

### 6. Optional n8n orchestration

The backend already performs analysis, reminders and e-mail. n8n is provided for people who want to edit reminder rules visually or route alerts elsewhere.

- `01 Bill Analysis` and `02 Deadline Reminders` workflows live in `n8n/`.
- Leaving `N8N_WEBHOOK_URL` blank uses the built-in local pipeline.
- Internal routes use shared secrets and are blocked from the public ingress.

---

## How it works

```text
React (Vite + Chart.js)
        │  /api
        ▼
FastAPI
  ├─ OCR: OpenCV + Tesseract (+ pdfplumber for text PDFs)
  ├─ Parser: deterministic rules for Indian bills
  ├─ Local model: JSON extraction, deep analysis, chat wording
  ├─ ML: hybrid forecast, robust anomaly detection, explainable tips
  ├─ SMTP: deadline reminders + new-alert e-mails
  └─ SQLAlchemy → MySQL / MariaDB (SQLite only for tests/demo)

Optional: n8n calls internal backend endpoints with a shared secret.
```

**The accuracy pipeline for chat:**

```text
Your bills → exact fact sheet → local model writes answer
           → Python numeric + semantic guard
           → one repair rewrite if needed
           → drop unverified sentences → show answer
```

The model never decides the totals. It only writes the wording around figures Python already calculated.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | React, Vite, Chart.js | Fast local dev, responsive UI, clear charts |
| Backend | FastAPI, Pydantic, SQLAlchemy | Typed validation, async-friendly, explicit ORM queries |
| Database | MySQL/MariaDB primary; SQLite fallback | Durable production schema; throwaway test/demo database |
| Auth | Argon2id, pinned HS256 JWT, signed CSRF | Short sessions, revocation, no password reuse |
| OCR | Tesseract + OpenCV + Poppler | Local, offline, handles photos and text PDFs |
| Local AI | Ollama | Runs small instruct models on CPU with no cloud calls |
| Forecasting | pandas, scikit-learn | Local hybrid model with walk-forward backtesting |
| E-mail | Native SMTP | No external automation service required |
| Optional automation | n8n | Visual workflow editing if wanted |
| Launchers | `run.ps1`, `run.cmd`, `run.sh` | One-command startup on Windows, Linux, macOS and WSL |
| Containers | Docker Compose + nginx | Optional isolated deployment |

---

## Repository map

```text
smart-bill-ai/
├─ backend/
│  ├─ app/
│  │  ├─ routers/          # auth, bills, analytics, AI, notifications, internal routes
│  │  ├─ services/         # OCR, parser, LLM, e-mail, reminders, bulk import
│  │  ├─ ml/               # forecast, anomaly detection, profile prior
│  │  ├─ config.py         # settings + secrets loading + production guard
│  │  ├─ database.py       # migration ledger and boot checks
│  │  ├─ main.py           # FastAPI app, lifespan, middleware
│  │  └─ security.py       # JWT, CSRF, Argon2, rate limits, client IP
│  ├─ db/mysql_schema.sql  # authoritative versioned schema (001–005)
│  ├─ scripts/             # setup, seed, migrate, backup, restore, evaluate
│  ├─ tests/               # SQLite + MariaDB regression suite
│  └─ requirements.txt
├─ frontend/
│  ├─ src/pages/           # AskAI, Upload, Bills, Insights, Settings, etc.
│  ├─ src/components/      # Markdown renderer, charts, shared UI
│  └─ package.json
├─ n8n/                    # optional workflow JSON
├─ run.ps1                 # PowerShell launcher (preferred on Windows)
├─ run.cmd                 # cmd launcher
├─ run.sh                  # Unix / Git Bash launcher
├─ docker-compose.yml      # optional container stack
├─ .env.example            # template only — never contains real secrets
├─ SETUP-WINDOWS.md        # machine-specific setup and troubleshooting
└─ HANDOFF.md              # verification and migration handoff record
```

---

## Quick start — Windows (native)

This is the verified local handoff path. No Docker is required.

### 1. Install prerequisites

- Python 3.11+
- Node.js LTS
- XAMPP (MariaDB/MySQL)
- Tesseract OCR with `eng` language data
- Poppler (`pdftoppm`, `pdftocairo`)
- Ollama with one small local instruct model: `ollama pull <model-name>`

### 2. Create a protected secrets file outside the repository

```powershell
mkdir "$env:USERPROFILE\.smartbill"
copy .env.example "$env:USERPROFILE\.smartbill\handoff-secrets.env"
```

Edit that file and replace **every** placeholder with a unique random value. Generate each secret separately, for example:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Never commit `.env` or the external secrets file. The backend and launchers load secrets in this order:

```text
real environment variables → external secrets file → optional project .env → defaults
```

Override the default file path with `SMARTBILL_SECRETS_FILE` if needed.

### 3. Create the database and apply the schema

```powershell
cd backend
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
$env:MYSQL_ROOT_PASSWORD = "<your MariaDB root password>"
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "<strong app password>"
cd ..
```

`setup_mysql.py` creates the `smartbill` database, a limited application user, applies ordered migrations from `backend/db/mysql_schema.sql` and prints a masked `DATABASE_URL` to put in your secrets file.

### 4. Install frontend dependencies

```powershell
cd frontend
npm install
cd ..
```

### 5. Launch

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
# or: .\run.cmd
```

Open <http://localhost:5173>. API docs are at <http://localhost:8000/api/docs> in development.

To seed demo data on first run: `.\run.cmd -Seed`, then log in with `demo@smartbill.ai` / `Demo@12345`.

Press **Ctrl+C** to stop everything. In `cmd.exe` you may see `Terminate batch job (Y/N)?`; answer **Y**. That exit code 1 is the batch shell stopping, not an app failure. Launching `run.ps1` directly from PowerShell avoids the prompt.

---

## Quick start — Docker

Provide the variables from `.env.example` through your shell or an external secrets file, then:

```bash
docker compose up -d --build
docker compose exec backend python -m scripts.seed   # optional demo data
```

- App: <http://localhost:8080>
- n8n editor: <http://localhost:5678> (only when the `n8n` profile is enabled)
- Local AI container: `docker compose --profile ai up -d`
- Optional n8n: `docker compose --profile n8n up -d`

The backend port is not published to the host; nginx is the only ingress.

---

## Configuration

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` or `production`; production refuses unsafe settings |
| `DATABASE_URL` | MySQL/MariaDB connection; SQLite is tests/demo only |
| `SECRET_KEY` | JWT signing secret; unique random value, 32+ characters |
| `JWT_PREVIOUS_SECRET_KEYS` | Old signing keys for rotation, each also 32+ and unique |
| `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_KEY_ID` | Pinned token identity |
| `RATE_LIMIT_SECRET` | Keyed HMAC for rate-limit buckets, 32+ characters |
| `INTERNAL_API_KEY` | Shared secret for internal routes, 24+ characters |
| `N8N_WEBHOOK_SECRET`, `N8N_ENCRYPTION_KEY` | Only needed when using n8n |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Native e-mail delivery |
| `SMTP_STARTTLS`, `SMTP_SSL` | Require encrypted SMTP in production |
| `EMAIL_ENABLED`, `REMINDER_DAYS_BEFORE` | E-mail switch and exact reminder window |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_ANALYSIS_MODEL` | Local model endpoints; blank model uses the first installed one |
| `OLLAMA_WARMUP` | Warm models at startup; set `false` on low-RAM machines |
| `TRUST_PROXY_HEADERS`, `TRUSTED_PROXY_HOPS` | Enable only behind your own reverse proxy |
| `CORS_ORIGINS`, `COOKIE_SECURE`, `APP_URL` | Required for a safe HTTPS deployment |

`OLLAMA_ANALYSIS_MODEL` blank reuses the chat model. Leave `OLLAMA_MODEL` blank to use the first installed local model. Model names are configuration details; the product shown to users is always Smart Bill AI.

---

## Using the app

1. **Add a bill** — upload a PDF, image or phone photo; review the raw OCR text and extracted fields; correct anything wrong; confirm and save.
2. **Bills / Dashboard** — see upcoming and overdue bills, paid/unpaid state and per-bill analysis.
3. **Insights** — view monthly charts, category share, forecast range, anomalies and recommendations. Use **Analyze all bills** for a cached portfolio analysis.
4. **Ask AI** — ask questions in plain language. Chat keeps the last 12 messages and supports markdown and bill uploads.
5. **My home** — answer optional household questions to personalise the forecast; each question explains why it is asked.
6. **Settings** — enable e-mail, check SMTP status and use **Send me a test e-mail** to verify real delivery.

---

## Verification status

These checks were completed for the handoff:

| Check | Result |
|---|---|
| Backend suite on throwaway SQLite | **166 passed** |
| Backend suite on MariaDB `smartbill_test` | **166 passed** |
| Frontend production build | Passed |
| Live MySQL smoke: health, login, CSRF, dashboard, bill list | Passed |
| 15 MB upload cap (`413`) and real PDF upload/read/cleanup | Passed |
| Migration ledger `001`–`005` and schema objects | Verified |
| Backup → restore into `smartbill_restore_test` with exact counts | Passed |
| Secret/tree scan: no `.env`, no rotated secrets, no `dist/`, no `backups/` | Passed |
| Ollama connectivity and local generation | Verified by live run log |

**Honest limitations:**

- **Real SMTP delivery was not verified.** Tests mock e-mail and no SMTP credentials were supplied. Use **Settings → Send me a test e-mail** with your own account.
- **End-user answer quality was not separately reviewed.** Model connectivity and generation work; the numeric and semantic guards are covered by tests.
- The only test warning is Starlette's deprecated `anyio.abc.BlockingPortal` alias from the test client. It is not an application failure.

Tests never point at the live `smartbill` database. The MySQL target must end in `_test`; the suite drops and recreates tables there only.

---

## Security

- **Passwords:** Argon2id, strong-password rule, 128-character cap, timing-equalised login including corrupt hashes, five failures → 15-minute lock. Argon2 runs in FastAPI's threadpool behind a four-slot cap and three-second acquire timeout.
- **Sessions:** HS256 JWT in an httpOnly, SameSite=Lax cookie (`Secure` in production); `exp`, `iat`, `sub`, `iss`, `aud`, `jti`, `ver` and `csrf` are required; algorithm, key id, issuer and audience are pinned. Access tokens last 15 minutes. Logout, logout-all and password change increment `token_version` to revoke every device.
- **CSRF:** signed double-submit token plus `X-Requested-With` and explicit `Origin` checks. Internal n8n routes are excluded from browser CSRF.
- **Rate limits:** durable `rate_limit_hits` counters and `rate_limit_locks` rows. Login is limited per keyed-hashed account and per client address; unknown/invalid addresses use a stricter bucket. Buckets are keyed HMACs and serialised atomically. `429` responses carry `Retry-After`.
- **Proxy handling:** all `X-Forwarded-For` lines are joined, validated and read from the right across trusted hops. `X-Real-IP` is ignored. IPv4-mapped IPv6 is unwrapped; native IPv6 is grouped by `/64`.
- **Authorisation:** every bill and file query checks ownership; other users' IDs return 404.
- **Uploads:** magic-byte type checks, 15 MB cap, random server filenames, path-traversal checks, SHA-256 duplicate detection and authenticated-only serving.
- **SQL and input:** SQLAlchemy ORM only, Pydantic validation everywhere, CSV formula-injection neutralisation and escaped e-mail HTML.
- **Headers and privacy:** consistent CSP, nosniff, X-Frame-Options, no-referrer and no-store headers from nginx and FastAPI. One-click account/data erasure.
- **Fail-safe config:** placeholder or short secrets, weak rotation keys, weak rate-limit secret, empty issuer/audience, colliding key id or negative proxy hops are refused in every environment. Production additionally refuses SQLite, insecure cookies, wildcard CORS and unencrypted SMTP.

---

## Production checklist

`APP_ENV=production` is necessary but not sufficient. Before exposing Smart Bill AI:

1. Serve it over **HTTPS** through a trusted reverse proxy; set `COOKIE_SECURE=true`.
2. Use MySQL/MariaDB, never SQLite.
3. Set explicit `CORS_ORIGINS`; never `*`.
4. Generate unique random secrets for `SECRET_KEY`, `RATE_LIMIT_SECRET`, `INTERNAL_API_KEY` and, if used, n8n secrets. Rotation keys must also be unique and 32+ characters.
5. Use `SMTP_STARTTLS=true` (587) or `SMTP_SSL=true` (465). Plain SMTP is rejected.
6. Keep the app reachable only through the proxy and enable `TRUST_PROXY_HEADERS` with the correct `TRUSTED_PROXY_HOPS`.
7. Keep MySQL bound to loopback or a private network; the application user has only `SELECT, INSERT, UPDATE, DELETE` on `smartbill`.
8. Take encrypted off-site backups, include `backend/uploads`, and test a restore.
9. Verify real SMTP delivery using **Settings → Send me a test e-mail**.
10. Commit/tag the verified source so you have a known-good rollback point.

See [HANDOFF.md](HANDOFF.md) for the exact migration ledger and verification record.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: Refusing to start` | Weak, missing or placeholder secrets | Create/replace the external secrets file; generate unique values |
| `Terminate batch job (Y/N)?` after Ctrl+C | Normal `cmd.exe` batch prompt | Answer **Y**, or use `run.ps1` directly |
| `GET /v1/models 404` in logs | External OpenAI-compatible probe | Harmless; the app uses `/health` and `/api/health` |
| One `GET /api/tags 500` at Ollama startup | Model service still initialising | Ignore if subsequent calls return 200 |
| Port 8000 or 5173 already in use | Another process holds the port | Stop it or change the launcher port |
| Models reload on every request | `OLLAMA_NUM_CTX` changed between requests | Keep one fixed context size |
| E-mail not arriving | SMTP not configured, wrong App Password, or blocked port | Check Settings status and test e-mail; Gmail needs an App Password |
| Tests refuse to run | `SMARTBILL_TEST_DATABASE_URL` points at a non-test DB | Use a database ending in `_test` or unset the variable |

---

## Detailed docs

- [SETUP-WINDOWS.md](SETUP-WINDOWS.md) — exact Windows installation, configuration, tests and troubleshooting.
- [HANDOFF.md](HANDOFF.md) — verification results, migration ledger, backup/restore drill and security notes.
- [.env.example](.env.example) — every supported setting with safe defaults and comments.
- [backend/db/mysql_schema.sql](backend/db/mysql_schema.sql) — authoritative versioned schema.

---

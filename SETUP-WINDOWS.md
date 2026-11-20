# Windows setup (this machine)

Everything runs natively on Windows — no Docker, no paid APIs. This file records the
exact setup that was installed and how to start, stop, test and troubleshoot it.

## What is installed

| Component | Version | Location |
|---|---|---|
| Python | 3.11+ | installed per user; added to `PATH` |
| Node.js / npm | LTS | default install location |
| XAMPP / MariaDB (MySQL) | MariaDB 10.4+ | `C:\xampp\mysql\bin` (data in `C:\xampp\mysql\data`) |
| Ollama | current release | installed with the Ollama desktop app |
| Smart Bill AI model | any small local instruct model you pull | managed by Ollama |
| Tesseract | 5.x with `eng` language data | your local OCR tools folder |
| Tesseract language data | `eng`, `osd`, optional `hin` | same tools folder |
| Poppler (`pdftoppm`, `pdftocairo`) | recent release | same tools folder |
| Backend virtualenv | all `requirements.txt` installed | `backend\.venv-win` |
| Frontend dependencies | `npm install` done | `frontend\node_modules` |

Check Ollama at any time:

```powershell
ollama list
curl http://localhost:11434/api/tags
```

## Start the app

Double-click `run.cmd`, or from a PowerShell window in this folder:

```powershell
.\run.cmd
# demo data on first run (login demo@smartbill.ai / Demo@12345):
.\run.cmd -Seed
```

On Linux, macOS, WSL or Git Bash use `./run.sh` instead; it starts the same
Smart Bill AI stack and installs missing Python/Node dependencies on the first run.

What the launcher does:

1. adds Tesseract/Poppler/Node to `PATH`;
2. when `DATABASE_URL` uses MySQL, starts XAMPP MySQL if port 3306 is closed and waits for it;
3. starts `ollama serve` when the API is not already up, and warns if the model is missing;
4. starts FastAPI on <http://localhost:8000> with auto-reload;
5. waits for `/api/health`, then starts Vite on <http://localhost:5173>;
6. stops both services when you press **Ctrl+C**.

Open:

* App: <http://localhost:5173>
* API docs: <http://localhost:8000/api/docs>
* AI status: <http://localhost:8000/api/ai/status>

The first AI extraction after a reboot can take **30–60 seconds** on this CPU-only
laptop while the model loads into memory. Later requests are faster.

## Configuration

Configuration is read in this order: real environment variables, the protected secrets file
outside the project (`%USERPROFILE%\.smartbill\handoff-secrets.env` by default), then an
optional root `.env` for non-secret overrides. The backend and both launchers load the external
file automatically, so no `.env` is shipped. Set `SMARTBILL_SECRETS_FILE` to use a different
path on another machine. Replace every sample value below with your own.

```dotenv
# MySQL from XAMPP (created by scripts/setup_mysql.py):
DATABASE_URL=mysql+pymysql://smartbill:<password>@127.0.0.1:3306/smartbill
# SQLite fallback, only for a quick demo:
# DATABASE_URL=sqlite:///./smartbill.db
UPLOAD_DIR=./uploads

TESSERACT_CMD=C:/path/to/ocr-tools/Library/bin/tesseract.exe
TESSDATA_PREFIX=C:/path/to/ocr-tools/share/tessdata
POPPLER_PATH=C:/path/to/ocr-tools/Library/bin
OCR_LANG=eng

OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=<your-small-instruct-model>          # chat replies
OLLAMA_ANALYSIS_MODEL=<your-fast-local-model>     # deep analysis + upload extraction
OLLAMA_ENABLED=true
OLLAMA_EXTRACT=true
OLLAMA_TIMEOUT=300
# Speed: one fixed context/thread count keeps the model loaded between requests.
OLLAMA_NUM_CTX=6144
OLLAMA_NUM_THREAD=0        # 0 = auto (half the logical CPUs)
OLLAMA_KEEP_ALIVE=30m
OLLAMA_WARMUP=true

# Optional: leave empty to use the built-in local analysis.
N8N_WEBHOOK_URL=

# E-mail delivery (deadline reminders + unusual-bill alerts). Leave SMTP_HOST
# blank to keep alerts in-app only.
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=Smart Bill AI <alerts@example.com>
EMAIL_ENABLED=true
REMINDER_DAYS_BEFORE=1
APP_URL=http://localhost:5173
```

* `OCR_LANG=eng+hin` enables Hindi OCR (the language data is already installed).
* Leave `OLLAMA_NUM_CTX` fixed — changing it between requests makes Ollama reload the model.
* Increase `OLLAMA_TIMEOUT` only if very large bills still time out on this CPU.
* Keep the external secrets file private; it is not committed anywhere. **`.env` is never
  shipped in the handoff**: ship only `.env.example`, and keep real values in a secret manager
  or a protected file outside the project tree. The backend auto-loads
  `~/.smartbill/handoff-secrets.env` (override with `SMARTBILL_SECRETS_FILE`) before validating
  startup. Rotation is **mandatory**: the previous secrets were exposed in an earlier copy, so
  treat the MySQL root/app passwords, `SECRET_KEY`, `RATE_LIMIT_SECRET`, `INTERNAL_API_KEY`,
  `N8N_WEBHOOK_SECRET` and `N8N_ENCRYPTION_KEY` as compromised and replace every one before use.

**If you set `APP_ENV=production`**, the app refuses to boot unless it is behind HTTPS
(reverse proxy) with `COOKIE_SECURE=true`, explicit `CORS_ORIGINS`, a MySQL/MariaDB
`DATABASE_URL` (not SQLite), and fresh unique secrets of the lengths listed above; API docs
are disabled. When e-mail is enabled, use `SMTP_STARTTLS=true` (587) or `SMTP_SSL=true`
(465) — plain SMTP is rejected in production. XAMPP on `127.0.0.1` is a development setup,
not an internet-facing production database.

### Database (MySQL via XAMPP)

Bills, alerts, profiles and AI insights live in **MySQL/MariaDB** served by XAMPP.
The schema is a versioned, reviewed file, `backend/db/mysql_schema.sql`; `setup_mysql.py`
applies its ordered steps as root and the backend checks them at startup. There is no manual
SQL and no drift between machines.

**Local XAMPP hardening (do this once, before exposing the API to anything but localhost):**

1. Open `C:\xampp\mysql\bin\my.ini`, find the `[mysqld]` section and make sure it contains
   `bind-address=127.0.0.1` (do **not** use `0.0.0.0` or `*`). Restart MySQL from the XAMPP
   Control Panel.
2. Set a root password (XAMPP defaults to blank):
   `C:\xampp\mysql\bin\mysqladmin.exe -u root password "<strong-unique-password>"`.
   Set `$env:MYSQL_ROOT_PASSWORD` before **every** root-using maintenance command
   (`setup_mysql.py`, `scripts.restore`, etc.); keep it outside the handoff tree, never in a
   shipped `.env`.
3. `setup_mysql.py` creates the app user with **only** `SELECT, INSERT, UPDATE, DELETE` on
   `smartbill`; all schema/DDL work is done as root. A `*_test` database gets full rights so
   the test suite can drop and recreate its tables.

**First-time setup** (XAMPP Control Panel → **Start MySQL** first):

```powershell
cd backend
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "choose-a-strong-password"
```

It creates:

* database `smartbill` with `utf8mb4_unicode_ci`;
* user `smartbill@localhost` / `@127.0.0.1` with only `SELECT, INSERT, UPDATE, DELETE` on
  that database (the app never connects as root);
* all tables, foreign keys and indexes, plus one `schema_migrations` row per applied version
  with its description and timestamp.

The printed `DATABASE_URL` is **masked** (password shown as `***`) — use the password you
supplied. Useful flags: `--start-xampp` (start MySQL if closed), `--reset-password` (change
an existing app user's password — nothing changes without it), `--replace --yes` (drop +
rebuild tables, preceded by an automatic `mysqldump` backup and an explicit confirmation),
`--database smartbill_test` (a separate database for MySQL test runs), and `--retain N`
(how many automatic backups to keep).

**Migrations and boot behaviour.** The SQL file holds version `001` (base tables),
`002` (`alerts.dedupe_key` plus `UNIQUE(user_id, dedupe_key)`, with duplicate legacy rows
removed first keeping the lowest id), `003` (`users.token_version` and `users.is_active`
for session revocation and account disable), `004` (`rate_limit_hits`, the shared durable
rate-limit table), and `005` (`rate_limit_locks`, one lock row per bucket for atomic counting).
`init_db()` fails fast if the file is missing, creates
the ledger and base tables when absent, applies pending versions in order, and refuses to
start on an unknown version or a failed migration (the error names the version). SQLite uses
`Base.metadata.create_all` and never runs MySQL `ALTER`s. Startup also refuses a recorded
`002`, `003`, `004` or `005` whose objects are missing. After a code
upgrade, set the admin credential and re-run the provisioner to apply pending migrations
(no `--replace` needed):

```powershell
$env:MYSQL_ROOT_PASSWORD = "<the root password you set>"
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "…"
```

Check with `SELECT version, description, applied_at FROM schema_migrations ORDER BY version;`.

**Moving the old SQLite data** (already done on this machine):

```powershell
.\.venv-win\Scripts\python.exe -m scripts.migrate_sqlite_to_mysql --replace
```

The script copies `users → household_profiles → bills → alerts → ai_insights` keeping
ids, then verifies every column of every row, row counts, ₹ totals, paid/unpaid counts,
foreign keys, JSON validity and `AUTO_INCREMENT`. It refuses to continue on any
mismatch. Rows that break the new foreign keys are reported before copying (this run:
one alert pointed at a deleted bill, so its `bill_id` became `NULL`). `--verify-only`
re-checks an existing copy without writing.

**Why the schema looks like this**

* InnoDB + real foreign keys: deleting an account cascades to its profiles, bills,
  insights and alerts; deleting a bill keeps its alerts (`bill_id = NULL`) but removes
  the per-bill AI cache.
* Money is `DECIMAL(12,2)` — `SUM(amount)` stays exact. Units/confidence are `DOUBLE`
  because they are measurements, not currency.
* `category`, alert `kind`/`severity` and insight `kind` are `ENUM`s: compact storage and
  the database itself rejects invalid values.
* Upload de-duplication is a `UNIQUE(user_id, file_hash)` key, so two identical files can
  never both be saved even if two requests race.
* `bills` indexes follow the real queries: `(user_id, due_date)` for month views,
  `(user_id, category, due_date)` for category filters/forecasts,
  `(user_id, reviewed, paid, due_date)` for dashboard totals, `(user_id, created_at)` for
  newest bills, and `(reviewed, paid, due_date, reminded_at)` for the hourly reminder
  scan. `alerts` has `(user_id, is_read, id)` for the bell and
  `(bill_id, kind, created_at)` for reminder/anomaly de-duplication; `ai_insights` has
  `(user_id, kind, bill_id, id)` for the cache lookup.
* Sessions are signed JWT cookies, so there is no session table to grow or clean up. Each token carries a `ver` claim checked against `users.token_version`; `POST /api/auth/logout`, `POST /api/auth/logout-all` and `POST /api/auth/change-password` bump it to revoke every device at once. Access tokens last 15 minutes with 10 s of clock-skew leeway. Writes require both the custom header and the signed double-submit `csrf_token` cookie; an unexpected `Origin` is rejected.
* Rate-limit counters live in `rate_limit_hits` (not process memory), so multiple Gunicorn/Uvicorn workers and restarts share the same buckets; `rate_limit_locks` serialises each bucket with `SELECT ... FOR UPDATE` on MySQL. Login is limited to 5 attempts/15 min per normalised, keyed-hashed account **and** 20/15 min per client address (3/15 min when no valid peer address exists); `429` responses include `Retry-After`. Behind a trusted proxy set `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXY_HOPS` to the number of app-facing hops — all `X-Forwarded-For` lines are joined and parsed from the right, `X-Real-IP` is ignored, invalid entries fall back to the socket peer, IPv4-mapped IPv6 is unwrapped and native IPv6 is grouped by `/64`. Do not expose the backend directly to the internet in that mode.
* Connections run with `time_zone='+00:00'` and store UTC `DATETIME(6)`; the UI converts
  to local dates.

**Backup and restore**

```powershell
cd backend
.\.venv-win\Scripts\python.exe -m scripts.backup                  # -> ..\backups\smartbill_YYYYmmdd_HHMMSS.sql
.\.venv-win\Scripts\python.exe -m scripts.backup --retain 14 --uploads
```

`backup` keeps the newest 7 dumps by default, optionally copies `uploads_<timestamp>`,
and passes credentials through a temporary defaults-extra-file plus `MYSQL_PWD` — never on
the command line or in logs. `restore` needs admin rights to create the target database:

```powershell
$env:MYSQL_ROOT_PASSWORD = "<the root password>"      # or type it at the prompt
.\.venv-win\Scripts\python.exe -m scripts.restore `
  --file ..\backups\smartbill_YYYYmmdd_HHMMSS.sql `
  --database smartbill_restore_test --yes
```

`restore` refuses non-`*_test` targets unless `--allow-live` is given, prints the masked
target URL before importing, and accepts `MYSQL_ADMIN_PASSWORD` as an alias for
`MYSQL_ROOT_PASSWORD`. Its printed counts come from `information_schema.TABLE_ROWS` and are
**estimates**, not exact `COUNT(*)` values.

**Restore test (recommended, safe):** the command above imports into the throwaway
`smartbill_restore_test` database, not into `smartbill`. The printed counts are estimates;
for a strict check run `SELECT COUNT(*)` on the restored tables and compare with the live
database, then drop the test database:
`C:\xampp\mysql\bin\mysql.exe -u root -p -e "DROP DATABASE smartbill_restore_test;"`.
Uploaded bill files stay on disk — add `--uploads` to backups and `--uploads-from
<backup>\uploads_<timestamp>` to restores when you need them.

### Choosing models

Two models can be used. `OLLAMA_MODEL` writes chat replies; a slightly larger local
instruct model gives the best wording, but on a laptop CPU its first answer after a restart
can take a couple of minutes while the prompt is processed. `OLLAMA_ANALYSIS_MODEL` runs
deep analysis, the portfolio summary and upload extraction; a smaller local model keeps
those fast and fits comfortably in 8 GB RAM. Leave `OLLAMA_ANALYSIS_MODEL` blank to reuse
the chat model. With `OLLAMA_MODEL=` blank the app uses the first model returned by
`ollama list`. Browse <https://ollama.com/library>, then run `ollama pull <model-name>`
for each model you want to keep.

If you keep several models and want specific ones, set `OLLAMA_MODEL=<model-name>` and
`OLLAMA_ANALYSIS_MODEL=<model-name>` in the external secrets file (or root `.env`) and restart
the backend. Leave them blank
to go back to automatic selection.

**The model writes, Python verifies.** Every chat reply is written by the model from an
exact fact sheet calculated from your bills (totals, unpaid bills, latest bill, categories,
recent bills and month totals). Before display, a numeric guard checks every ₹ amount,
percentage, date and year. If the model quotes an unverified figure it is asked once to
rewrite; sentences that still contain one are dropped, so a wrong number cannot reach the
screen. Plain arithmetic and percentages are checked with Python's exact calculator. The
"Analyze all bills" and per-bill cards pass the same numeric check, and their monthly-saving
figures are computed from your bills, never chosen by the model. A larger model improves
wording, but the figures are still verified independently.

### E-mail setup

Deadline reminders and unusual-bill alerts are sent directly by the backend over SMTP.
Fill the `SMTP_*` values in the external secrets file (or root `.env`); with Gmail use an App
Password (Google Account → Security → 2-Step Verification → App passwords), not your normal
password. Restart the
backend, open **Settings**, and click **Send me a test e-mail** to verify. If `SMTP_HOST`
is blank, e-mail is skipped and alerts remain visible in the app.

## Run the tests

The OCR tests need Tesseract on `PATH`:

```powershell
cd backend
$env:Path = "C:\path\to\ocr-tools\Library\bin;" + $env:Path
.\.venv-win\Scripts\python.exe -m pytest -q
```

Verified: **166 passed** on SQLite and **166 passed** on MariaDB `smartbill_test` (the
MySQL command below). The SQLite run uses a temporary database; the MySQL run uses a
dedicated `*_test` database only and never `smartbill`. Both mock the model and SMTP, so
neither Ollama nor a real e-mail account is exercised. SMTP delivery was **not** verified in
this handoff; use the **Send me a test e-mail** button in Settings on your own account.

Run the same suite on the production engine (MariaDB, optional):

```powershell
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --database smartbill_test --app-password "…"
$env:SMARTBILL_TEST_DATABASE_URL = "mysql+pymysql://smartbill:…@127.0.0.1:3306/smartbill_test"
.\.venv-win\Scripts\python.exe -m pytest -q
Remove-Item Env:\SMARTBILL_TEST_DATABASE_URL
```

The suite drops and recreates the tables in that database, so the name must end in
`_test` (the guard refuses anything else). The live `smartbill` database is never touched
by the test suite.

Useful scripts:

```powershell
cd backend
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "…"   # create DB + user + apply pending migrations
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "…" --reset-password  # rotate app password explicitly
.\.venv-win\Scripts\python.exe -m scripts.backup          # dump + retention (see above)
.\.venv-win\Scripts\python.exe -m scripts.restore --file ..\backups\<dump>.sql --database smartbill_restore_test --yes
.\.venv-win\Scripts\python.exe -m scripts.migrate_sqlite_to_mysql --replace  # copy SQLite -> MySQL (verified)
.\.venv-win\Scripts\python.exe -m scripts.seed            # create/refresh demo data
.\.venv-win\Scripts\python.exe -m scripts.evaluate        # parser/forecast accuracy benchmark
.\.venv-win\Scripts\python.exe -m scripts.ocr_debug C:\path\to\bill.jpg --save-dir C:\Temp\ocr
```

## Troubleshooting

**Upload says OCR is unavailable / Tesseract reports 0 languages**
Check both paths in the external secrets file (or root `.env`); portable builds keep
`traineddata` outside `Library\bin`.
`TESSDATA_PREFIX` must point at the folder that directly contains `eng.traineddata`.

**PDF upload fails with "Unable to get page count"**
`POPPLER_PATH` must point at the folder containing `pdftoppm.exe`
(for example, `C:\path\to\ocr-tools\Library\bin`).

**AI status is unavailable**
Start Ollama (`ollama serve`) and make sure the model is pulled (`ollama list`).
The app still works without it: the regex parser extracts the common fields.

**Slow first extraction**
Normal on CPU. The backend warms the model at startup (`OLLAMA_WARMUP=true`) and keeps it
loaded for 30 minutes (`OLLAMA_KEEP_ALIVE=30m`). Close other heavy apps, stay on mains power,
and prefer a smaller model on battery. Exact bill answers appear instantly; answers that need
the model are buffered briefly and checked before any text is shown.

**“Analyze all bills” shows a quick summary instead of the model's write-up**
The model hit the time limit or was cut off. The app returns an instant built-in summary
marked as such instead of an error. Click **Re-run analysis**; if it keeps happening, use a
smaller model or reduce the number of bills.

**API fails with "Can't connect to MySQL server on '127.0.0.1'"**
MySQL is not running. Start it in the XAMPP Control Panel (or run `setup_mysql.py
--start-xampp`); `run.cmd` starts it automatically when `DATABASE_URL` is MySQL.
Check with `C:\xampp\mysql\bin\mysqladmin.exe -u root ping`.

**API fails with "Access denied for user 'smartbill'"**
The password in `DATABASE_URL` no longer matches the MySQL account. Re-run
`python -m scripts.setup_mysql --app-password "<the new password>" --reset-password`
(the explicit flag changes the account password) and update `DATABASE_URL` in the external
secrets file (or root `.env`). Ordinary runs without
`--reset-password` never change a live password.

**MySQL data looks empty after switching from SQLite**
The SQLite fallback file is `backend\smartbill.db`; import it once with
`python -m scripts.migrate_sqlite_to_mysql --replace`.

**Port already in use**
Another backend/frontend is still running. Stop it, or start on other ports:
`$env:BACKEND_PORT=8001; $env:FRONTEND_PORT=5174; .\run.ps1` (the Vite proxy follows
`BACKEND_PORT` automatically through the launcher).

**Rebuild the backend environment**

```powershell
cd backend
Remove-Item -Recurse -Force .venv-win
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install --upgrade pip
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
```

**Reinstall frontend dependencies**

```powershell
cd frontend
Remove-Item -Recurse -Force node_modules
npm install
```

## n8n (optional)

n8n only orchestrates reminders/alerts; it is **not** required for upload, OCR or AI
analysis. With `N8N_WEBHOOK_URL=` empty the backend generates alerts locally. To enable
it later, install n8n (Node.js is already available), start it on port 5678, import
`n8n/01-bill-analysis.json`, and set:

```dotenv
N8N_WEBHOOK_URL=http://localhost:5678/webhook/bill-analysis
```

Compose sets `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` because those workflow Code nodes read
`BACKEND_URL`, `INTERNAL_API_KEY`, `N8N_WEBHOOK_SECRET` and `SMTP_FROM` from `$env`. The
safer production default (`true`) breaks them until the values are moved into n8n
credentials. Keep the editor bound to `127.0.0.1` and rotate the shared secrets if they were
ever pasted into a workflow.

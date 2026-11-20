# Smart Bill AI — final handoff status

## Verified

| Check | Result |
|---|---|
| Backend test suite on throwaway SQLite | **166 passed**, 1 dependency warning |
| Backend test suite on MariaDB `smartbill_test` | **166 passed**, 1 dependency warning |
| Frontend production build (`npm run build`) | Passed |
| MySQL smoke: health, demo login, dashboard, bill list | Passed |
| MySQL smoke: 15 MB API upload cap (`413`) and real PDF upload/cleanup | Passed |
| Auth smoke: CSRF cookie, CSRF mismatch rejection, session-revocation schema | Passed |
| Migration ledger (`001`–`005`) and auth/alert/rate-limit schema on `smartbill` | Verified |
| Backup → restore into `smartbill_restore_test`, exact-count comparison, test DB dropped | Passed |
| Secret/tree scan (rotated values absent, no `.env`, no `dist/`, no `backups/`) | Passed |

The only warning is Starlette’s deprecated `anyio.abc.BlockingPortal` alias from the test
client. It is not an application failure.

## Not verified

- **Real SMTP delivery.** No SMTP credentials were supplied. Tests mock e-mail; use
  **Settings → Send me a test e-mail** on the operator’s own account.
- **Live model output.** Tests mock AI and the smoke check disables background model
  warm-up. The local fallback and numeric/semantic guards are covered by tests.

## Database and migrations

- MariaDB/MySQL is primary; SQLite remains for tests and the zero-setup demo.
- `backend/db/mysql_schema.sql` is authoritative with ordered migrations:
  - `001` — base tables
  - `002` — `alerts.dedupe_key` and `uq_alerts_user_dedupe`, with legacy duplicates removed
  - `003` — `users.token_version` and `users.is_active` for session revocation/disable
  - `004` — `rate_limit_hits`, durable counters shared by every worker/restart
  - `005` — `rate_limit_locks`, one row per bucket for atomic `SELECT ... FOR UPDATE` counting
- `schema_migrations` records version, description and timestamp. Unknown versions, gaps,
  a missing schema file, a missing `002`/`003`/`004`/`005` object, or a failing statement fail startup loudly.
- Apply pending migrations as an admin before restarting the app:

```powershell
cd backend
$env:MYSQL_ROOT_PASSWORD = "<root password from your secret store>"
.\.venv-win\Scripts\python.exe -m scripts.setup_mysql --app-password "<app password>"
```

The application user has only `SELECT, INSERT, UPDATE, DELETE` on `smartbill`; `smartbill_test`
retains full rights so the suite can recreate tables. The test fixture drops both the base
tables and `schema_migrations`, so repeated MariaDB runs do not skip `001`.

## Backup and restore

```powershell
cd backend
.\.venv-win\Scripts\python.exe -m scripts.backup --retain 14 --uploads
$env:MYSQL_ROOT_PASSWORD = "<root password from your secret store>"
.\.venv-win\Scripts\python.exe -m scripts.restore `
  --file ..\backups\smartbill_YYYYmmdd_HHMMSS.sql `
  --database smartbill_restore_test --yes
```

`restore` refuses a non-`*_test` target without `--allow-live`. Its printed counts are
`information_schema.TABLE_ROWS` estimates; confirm with `SELECT COUNT(*)` for an exact check.
Drop `smartbill_restore_test` after verification.

## Security and secrets

- `.env` is **not present** in this handoff tree. Only `.env.example` is shipped.
- The backend and both launchers auto-load the protected secrets file below before startup;
  precedence is real environment variables → external file → optional project `.env` → defaults.
  Set `SMARTBILL_SECRETS_FILE` to use a different path on another machine. A missing/placeholder
  `SECRET_KEY` or `RATE_LIMIT_SECRET` now fails fast with an actionable launcher message.
- All previously exposed secrets were rotated. New values are stored outside the project at
  `~/.smartbill/handoff-secrets.env` with user-only ACL; they are not in this
  tree or in any command line/log. Treat the old values as compromised.
- XAMPP MariaDB now listens on `127.0.0.1:3306` only. The root password was set to the
  operator-supplied value, and root exists only for loopback hosts. Application grants are
  CRUD-only on `smartbill`.
- The same CSP is set by nginx (Docker) and FastAPI (native). `/api/internal/*` is blocked by
  nginx; n8n remains optional and its editor stays bound to localhost.
- Compose sets `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` intentionally because the bundled
  workflow Code nodes read shared values from `$env`. Prefer `true` plus n8n credentials in
  production.

## Auth hardening

- JWTs require `exp`, `iat`, `sub`, `iss`, `aud`, `jti`, `ver` and `csrf`; HS256, the `kid`,
  issuer and audience are pinned, with 10 s of leeway. Access tokens last 15 minutes. Signing
  always uses `jwt_keys[jwt_key_id]` — the same map used for verification — so rotation cannot
  silently diverge. A placeholder/short `SECRET_KEY` or rotation key, weak `RATE_LIMIT_SECRET`,
  empty issuer/audience, colliding/empty `JWT_KEY_ID` or negative `TRUSTED_PROXY_HOPS` is
  refused in every environment; `JWT_PREVIOUS_SECRET_KEYS` supports rotation through the
  token's `kid`, and `get_current_user` always compares `ver` to the DB `token_version`.
- Writes require `X-Requested-With`, a matching readable `csrf_token` cookie and
  `X-CSRF-Token` echoed from the signed JWT claim; unexpected `Origin` headers are rejected.
  The SPA sends the new header automatically.
- `POST /api/auth/logout`, `POST /api/auth/logout-all` and `POST /api/auth/change-password`
  increment `users.token_version` in the database, invalidating every device's session. There
  is no process-local denylist. Deleted, disabled and version-mismatched users are rejected on
  every request.
- Rate limits are durable in `rate_limit_hits`: 5/15 min per normalised, keyed-hashed account
  plus 20/15 min per client address (3/15 min for an unknown peer) for login. Buckets are
  keyed HMACs using `RATE_LIMIT_SECRET` and serialised by `rate_limit_locks` with
  `SELECT ... FOR UPDATE` on MySQL; `429` responses carry `Retry-After` and hits are logged.
  `TRUST_PROXY_HEADERS`/`TRUSTED_PROXY_HOPS` join all `X-Forwarded-For` lines and parse them
  from the right with validated IPs; `X-Real-IP` is ignored, invalid entries fall back to the
  socket peer, IPv4-mapped IPv6 is unwrapped and native IPv6 is grouped by `/64`. Configure it
  only when the app is exclusively reachable through your own proxy.
- Argon2 runs in FastAPI's threadpool behind a 4-slot concurrency cap with a 3-second acquire
  timeout; missing or corrupt stored hashes still perform a dummy verification. Hashes are
  upgraded on successful login when parameters change. Cookies are deleted with the same
  attributes used to set them.

## Tree notes

- `frontend/dist/` is intentionally absent: the launchers run Vite and Docker rebuilds it.
  `npm run build` was verified immediately before cleanup.
- Verification dumps/test databases were removed after the restore drill.
- The verified project is committed to `https://github.com/meetgajjar04/Smart-Bill` on
  branch `main`. No `.env`, secrets, virtualenvs, build output or backup files are included.

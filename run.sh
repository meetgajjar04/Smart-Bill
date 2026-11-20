#!/usr/bin/env bash
# Smart Bill AI - start the complete app with one command.
# Works on Linux, macOS, WSL and Git Bash (Windows).
#
#   ./run.sh              start Smart Bill AI (backend + frontend)
#   ./run.sh --seed       reload demo data, then start
#   ./run.sh --install    reinstall backend + frontend dependencies, then start
#   ./run.sh --help
#
# Press Ctrl+C to stop everything.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
ENV_FILE="$ROOT/.env"
# Secrets are kept outside the project tree. Set SMARTBILL_SECRETS_FILE to use
# another location; the backend applies the same precedence.
SECRETS_FILE="${SMARTBILL_SECRETS_FILE:-$HOME/.smartbill/handoff-secrets.env}"

SEED=0
INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --seed|-Seed) SEED=1 ;;
    --install) INSTALL=1 ;;
    --help|-h) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (use --help)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Read one value without executing either file. Precedence matches the backend:
# real environment, external secrets file, project .env.
env_value() {
  local key="$1" line file
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return 0; fi
  for file in "$SECRETS_FILE" "$ENV_FILE"; do
    [ -f "$file" ] || continue
    line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1)" || continue
    [ -n "$line" ] || continue
    line="${line#*=}"
    line="${line%$'\r'}"
    line="${line#\"}"; line="${line%\"}"
    line="${line#\'}"; line="${line%\'}"
    if [ -n "$line" ]; then printf '%s' "$line"; return 0; fi
  done
  return 0
}

for key in SECRET_KEY RATE_LIMIT_SECRET; do
  value="$(env_value "$key")"
  case "$value" in
    ""|change-me*|changeme*|dev-*|replace-me*|your-*|example-*|*at-least-32*|*placeholder*)
      die "Missing or placeholder $key. Create $SECRETS_FILE or set it in the environment/project .env."
      ;;
  esac
done

BACKEND_PORT="${BACKEND_PORT:-$(env_value BACKEND_PORT)}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-$(env_value FRONTEND_PORT)}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OLLAMA_URL="${OLLAMA_URL:-$(env_value OLLAMA_URL)}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_URL="${OLLAMA_URL%/}"
OLLAMA_PROBE="$(printf '%s' "$OLLAMA_URL" | sed 's#//localhost#//127.0.0.1#')"

# OCR binaries configured in .env (the backend also reads .env itself).
for key in TESSERACT_CMD TESSDATA_PREFIX POPPLER_PATH; do
  value="$(env_value "$key")"
  [ -n "$value" ] && export "$key=$value"
done
POPPLER_DIR="$(env_value POPPLER_PATH)"
[ -n "$POPPLER_DIR" ] && [ -d "$POPPLER_DIR" ] && PATH="$POPPLER_DIR:$PATH"

# ---------------------------------------------------------------- prerequisites
find_python() {
  local candidate
  for candidate in \
    "$BACKEND/.venv-win/Scripts/python.exe" \
    "$BACKEND/.venv-win/bin/python" \
    "$BACKEND/.venv/Scripts/python.exe" \
    "$BACKEND/.venv/bin/python"; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

PY="$(find_python || true)"
if [ "$INSTALL" -eq 1 ] || [ -z "$PY" ]; then
  if [ -z "$PY" ]; then
    BASE_PY="$(command -v python3 || command -v python || true)"
    [ -n "$BASE_PY" ] || die "Python 3 was not found. Install Python 3.11+ and run this file again."
    say "Creating the backend virtual environment..."
    "$BASE_PY" -m venv "$BACKEND/.venv" || die "Could not create backend/.venv"
    PY="$(find_python || true)"
    [ -n "$PY" ] || die "Could not find the Python interpreter inside backend/.venv"
  fi
  say "Installing backend dependencies..."
  "$PY" -m pip install --upgrade pip || warn "Could not upgrade pip; continuing."
  "$PY" -m pip install -r "$BACKEND/requirements.txt" || die "Backend dependency installation failed."
fi

if ! command -v npm >/dev/null 2>&1 && ! command -v npm.cmd >/dev/null 2>&1; then
  for node_dir in "/c/Program Files/nodejs" "/c/Program Files (x86)/nodejs"; do
    [ -d "$node_dir" ] && PATH="$node_dir:$PATH" && break
  done
fi
NPM="$(command -v npm || command -v npm.cmd || true)"
[ -n "$NPM" ] || die "Node.js/npm was not found. Install Node.js LTS and run this file again."

if [ "$INSTALL" -eq 1 ] || [ ! -d "$FRONTEND/node_modules" ]; then
  say "Installing frontend dependencies (this can take a minute)..."
  (cd "$FRONTEND" && "$NPM" install --no-audit --no-fund) || die "Frontend dependency installation failed."
fi

command -v curl >/dev/null 2>&1 || die "curl is required to start and health-check Smart Bill AI."
command -v tesseract >/dev/null 2>&1 || warn "Tesseract not found - photo/PDF bills will need manual entry."
command -v pdftoppm >/dev/null 2>&1 || warn "Poppler not found - PDF bills cannot be rendered for OCR."

# ---------------------------------------------------------------- ports in use
port_busy() {
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$1" >/dev/null 2>&1 && return 0
    nc -z localhost "$1" >/dev/null 2>&1 && return 0
    return 1
  fi
  curl -s -o /dev/null --max-time 1 "http://localhost:$1/" 2>/dev/null
}

tcp_open() {
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$1" >/dev/null 2>&1
    return $?
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# ---------------------------------------------------------------- MySQL (XAMPP)
DB_URL_VALUE="$(env_value DATABASE_URL)"
case "$DB_URL_VALUE" in
  mysql*|mariadb*)
    DB_PORT="$(printf '%s' "$DB_URL_VALUE" | sed -nE 's#^[^:]+://([^@/]*@)?[^:/]+:([0-9]+)/.*#\2#p')"
    DB_PORT="${DB_PORT:-3306}"
    if ! tcp_open "$DB_PORT"; then
      MYSQLD=""
      for candidate in \
        /c/xampp/mysql/bin/mysqld.exe \
        /opt/lampp/sbin/mysqld \
        /Applications/XAMPP/xamppfiles/bin/mysqld; do
        [ -x "$candidate" ] && { MYSQLD="$candidate"; break; }
      done
      [ -n "$MYSQLD" ] || MYSQLD="$(command -v mysqld 2>/dev/null || true)"
      if [ -n "$MYSQLD" ]; then
        say "Starting MySQL on :$DB_PORT ..."
        nohup "$MYSQLD" --standalone >/dev/null 2>&1 &
      elif [ -x /opt/lampp/lampp ]; then
        say "Starting XAMPP MySQL ..."
        /opt/lampp/lampp startmysql >/dev/null 2>&1 || true
      else
        warn "MySQL is not running. Start it in XAMPP and run this file again."
      fi
      for _ in {1..60}; do tcp_open "$DB_PORT" && break; sleep 0.5; done
      tcp_open "$DB_PORT" || die "MySQL did not start on port $DB_PORT."
    fi
    ;;
esac

EXTERNAL_BACKEND=0
EXTERNAL_FRONTEND=0
BACKEND_PID=""
FRONTEND_PID=""
OLLAMA_PID=""

if port_busy "$BACKEND_PORT"; then
  if curl -sf -m 2 "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    say "Smart Bill AI backend is already running on :$BACKEND_PORT."
    EXTERNAL_BACKEND=1
  else
    die "Port $BACKEND_PORT is in use by another program. Stop it or set BACKEND_PORT."
  fi
fi
if port_busy "$FRONTEND_PORT"; then
  if curl -sf -m 2 "http://localhost:$FRONTEND_PORT/" >/dev/null 2>&1; then
    say "Smart Bill AI frontend is already running on :$FRONTEND_PORT."
    EXTERNAL_FRONTEND=1
  else
    die "Port $FRONTEND_PORT is in use by another program. Stop it or set FRONTEND_PORT."
  fi
fi

# Nothing to do when both services are already up.
if [ "$EXTERNAL_BACKEND" -eq 1 ] && [ "$EXTERNAL_FRONTEND" -eq 1 ]; then
  echo
  say "  Smart Bill AI is already running"
  say "  App       : http://localhost:$FRONTEND_PORT"
  say "  API docs  : http://localhost:$BACKEND_PORT/api/docs"
  say "  Login     : demo@smartbill.ai / Demo@12345"
  exit 0
fi

# ---------------------------------------------------------------- Ollama engine
if ! curl -sf -m 2 "$OLLAMA_PROBE/api/tags" >/dev/null 2>&1; then
  if command -v ollama >/dev/null 2>&1; then
    say "Starting the Ollama engine for Smart Bill AI..."
    ollama serve >/dev/null 2>&1 &
    OLLAMA_PID=$!
    for _ in {1..20}; do
      curl -sf -m 1 "$OLLAMA_PROBE/api/tags" >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
fi
if curl -sf -m 2 "$OLLAMA_PROBE/api/tags" >/dev/null 2>&1; then
  AI_STATE="ready"
else
  AI_STATE="Ollama not reachable - Smart Bill AI will use the rule-based fallback"
fi

# ---------------------------------------------------------------- lifecycle
stopping=0
kill_tree() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v taskkill >/dev/null 2>&1; then
        taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
        return 0
      fi
      ;;
  esac
  if command -v pkill >/dev/null 2>&1; then
    pkill -TERM -P "$pid" 2>/dev/null || true
  fi
  if command -v setsid >/dev/null 2>&1; then
    kill -TERM -- "-$pid" 2>/dev/null || true
  fi
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  [ "$stopping" -eq 1 ] && return
  stopping=1
  if [ -z "$BACKEND_PID" ] && [ -z "$FRONTEND_PID" ] && [ -z "$OLLAMA_PID" ]; then
    return
  fi
  echo
  say "Shutting down Smart Bill AI..."
  kill_tree "$FRONTEND_PID"
  kill_tree "$BACKEND_PID"
  kill_tree "$OLLAMA_PID"
  wait 2>/dev/null || true
  say "Stopped."
}
trap cleanup EXIT INT TERM

if [ "$SEED" -eq 1 ]; then
  say "Loading demo data..."
  (cd "$BACKEND" && "$PY" -m scripts.seed) || warn "Demo data seeding failed; starting anyway."
fi

# ---------------------------------------------------------------- backend
if [ "$EXTERNAL_BACKEND" -eq 0 ]; then
  say "Starting Smart Bill AI backend on :$BACKEND_PORT ..."
  if command -v setsid >/dev/null 2>&1; then
    (cd "$BACKEND" && exec setsid "$PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT") &
  else
    (cd "$BACKEND" && exec "$PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT") &
  fi
  BACKEND_PID=$!

  printf 'Waiting for the backend '
  READY=0
  for _ in {1..60}; do
    if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$BACKEND_PORT/api/health"; then
      READY=1
      break
    fi
    kill -0 "$BACKEND_PID" 2>/dev/null || { echo; die "The backend stopped during startup. See the log above."; }
    printf '.'
    sleep 1
  done
  echo
  [ "$READY" -eq 1 ] || die "The backend did not become ready in time. See the log above."
  say "Smart Bill AI backend ready."
fi

# ---------------------------------------------------------------- frontend
if [ "$EXTERNAL_FRONTEND" -eq 0 ]; then
  say "Starting Smart Bill AI frontend on :$FRONTEND_PORT ..."
  (cd "$FRONTEND" && exec env VITE_PORT="$FRONTEND_PORT" VITE_API_PROXY="http://localhost:$BACKEND_PORT" "$NPM" run dev -- --port "$FRONTEND_PORT") &
  FRONTEND_PID=$!

  printf 'Waiting for the interface '
  READY=0
  for _ in {1..60}; do
    if curl -sf -o /dev/null --max-time 2 "http://localhost:$FRONTEND_PORT/"; then
      READY=1
      break
    fi
    kill -0 "$FRONTEND_PID" 2>/dev/null || { echo; die "The frontend stopped during startup. See the log above."; }
    printf '.'
    sleep 1
  done
  echo
  [ "$READY" -eq 1 ] || die "The interface did not become ready in time. See the log above."
fi

# ---------------------------------------------------------------- ready
echo
say "  Smart Bill AI is running"
say "  App       : http://localhost:$FRONTEND_PORT"
say "  API docs  : http://localhost:$BACKEND_PORT/api/docs"
say "  Login     : demo@smartbill.ai / Demo@12345"
say "  AI engine : $AI_STATE"
echo
say "Press Ctrl+C to stop everything."

while true; do
  if [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    warn "The backend stopped. Shutting everything down."
    break
  fi
  if [ -n "$FRONTEND_PID" ] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    warn "The frontend stopped. Shutting everything down."
    break
  fi
  sleep 1
done

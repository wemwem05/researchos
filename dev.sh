#!/usr/bin/env bash
# ResearchOS Milestone 7: unified local startup.
#
#   ./dev.sh
#
# Starts the Python workspace backend (127.0.0.1:8765) and the Next.js
# frontend (localhost:3000) together, shuts both down cleanly on Ctrl-C,
# and reuses an already-running backend instead of starting a duplicate.
#
# Ports: RESEARCHOS_BACKEND_PORT (default 8765), FRONTEND_PORT (default 3000).
# Separate commands stay available for debugging:
#   .venv/bin/python prototype/agent.py serve-all --port 8765
#   npm run dev --prefix web
set -euo pipefail
set -m  # job control: each background job gets its own process group, so
        # shutdown signals reach whole trees (npm wrapper + next server +
        # workers), never orphaning a port holder.

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PORT="${RESEARCHOS_BACKEND_PORT:-8765}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
# Must match SERVER_VERSION in prototype/workspace_server.py; bump both on any
# API/shape/CORS change so a stale reused backend fails loudly, not oddly.
EXPECTED_SERVER_VERSION="7.3"

BACKEND_PID=""
FRONTEND_PID=""
BACKEND_STARTED_BY_US=0

fail() { echo "[dev.sh] error: $1" >&2; exit 1; }
info() { echo "[dev.sh] $1" >&2; }

# PID (not command) of the current TCP listener on a port, if any.
# Always succeeds (prints nothing when free): with `set -o pipefail`, a bare
# `lsof | head` would fail on a free port and `set -e` would kill the script.
port_listener() {
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -n 1
  return 0
}

# True when a pid is gone — or a zombie awaiting wait (kill -0 alone would
# report zombies as alive and stall shutdown for the full grace period).
is_dead() {
  kill -0 "$1" 2>/dev/null || return 0
  command -v ps >/dev/null 2>&1 || return 1
  local st
  st="$(ps -p "$1" -o stat= 2>/dev/null | tr -d ' ')" || st=""
  [ -z "${st}" ] && return 0
  case "${st}" in Z*) return 0;; *) return 1;; esac
}

# TERM a whole process-group tree, then KILL survivors. $1 = group leader pid.
kill_tree() {
  local leader="$1"
  [ -n "${leader}" ] || return 0
  is_dead "${leader}" && return 0
  kill -TERM -- "-${leader}" 2>/dev/null || kill -TERM "${leader}" 2>/dev/null || true
  local i
  for i in $(seq 1 50); do
    is_dead "${leader}" && return 0
    sleep 0.1
  done
  kill -KILL -- "-${leader}" 2>/dev/null || kill -KILL "${leader}" 2>/dev/null || true
}

cleanup() {
  trap - INT TERM EXIT
  if [ -n "${FRONTEND_PID}" ] && kill -0 "${FRONTEND_PID}" 2>/dev/null; then
    info "stopping frontend tree (${FRONTEND_PID})…"
    kill_tree "${FRONTEND_PID}"
    wait "${FRONTEND_PID}" 2>/dev/null || true
  fi
  # Belt and braces: if our frontend port is still held, the holder is our
  # orphan (we started the frontend) — stop it so the next run can bind.
  if [ -n "${FRONTEND_PID}" ]; then
    local holder
    holder="$(port_listener "${FRONTEND_PORT}")"
    if [ -n "${holder}" ]; then
      info "port ${FRONTEND_PORT} still held by ${holder}; stopping it…"
      kill -TERM "${holder}" 2>/dev/null || true
      sleep 1
      kill -0 "${holder}" 2>/dev/null && kill -KILL "${holder}" 2>/dev/null || true
    fi
  fi
  if [ "${BACKEND_STARTED_BY_US}" = "1" ] && [ -n "${BACKEND_PID}" ] \
      && kill -0 "${BACKEND_PID}" 2>/dev/null; then
    info "stopping backend tree (${BACKEND_PID})…"
    kill_tree "${BACKEND_PID}"
    wait "${BACKEND_PID}" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

# -- prerequisites -----------------------------------------------------------
[ -x "${ROOT}/.venv/bin/python" ] || fail "no Python venv at .venv/ — run: python3 -m venv .venv && source .venv/bin/activate && pip install -r prototype/requirements.txt"
command -v npm >/dev/null 2>&1 || fail "npm is not installed (needed for the Next.js frontend)"
command -v node >/dev/null 2>&1 || fail "node is not installed (needed for the Next.js frontend)"
[ -f "${ROOT}/.env" ] || info "warning: no .env file — model calls will fail until MODEL_API_KEY/BASETEN_API_KEY are set (see .env.example)"

if [ ! -d "${ROOT}/web/node_modules" ]; then
  info "installing frontend dependencies (first run)…"
  (cd "${ROOT}/web" && npm install) || fail "npm install failed in web/"
fi

# -- backend (reuse a healthy one; never start a duplicate) -------------------
probe_backend() {
  "${ROOT}/.venv/bin/python" - "$1" <<'EOF' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
        body = json.load(r)
    sys.exit(0 if body.get("ok") else 1)
except Exception:
    sys.exit(1)
EOF
}

backend_version() {
  "${ROOT}/.venv/bin/python" - "$1" <<'EOF' 2>/dev/null
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=2) as r:
        print(json.load(r).get("data", {}).get("server_version", ""))
except Exception:
    print("")
EOF
}

if probe_backend "${BACKEND_PORT}"; then
  RUNNING_VERSION="$(backend_version "${BACKEND_PORT}")"
  if [ "${RUNNING_VERSION}" != "${EXPECTED_SERVER_VERSION}" ]; then
    HOLDER="$(port_listener "${BACKEND_PORT}")"
    if [ -n "${HOLDER}" ]; then
      fail "backend on 127.0.0.1:${BACKEND_PORT} is stale (server v${RUNNING_VERSION:-unknown}, this checkout wants v${EXPECTED_SERVER_VERSION}). Restart it: kill ${HOLDER} — then re-run ./dev.sh"
    fi
    fail "backend on 127.0.0.1:${BACKEND_PORT} is stale (server v${RUNNING_VERSION:-unknown}, this checkout wants v${EXPECTED_SERVER_VERSION}). Stop that backend and re-run ./dev.sh"
  fi
  info "backend already running on 127.0.0.1:${BACKEND_PORT} — reusing it"
else
  HOLDER="$(port_listener "${BACKEND_PORT}")"
  [ -n "${HOLDER}" ] && fail "port ${BACKEND_PORT} is held by PID ${HOLDER}, which is not a healthy ResearchOS backend. Stop it first (kill ${HOLDER}) or set RESEARCHOS_BACKEND_PORT."
  info "starting backend on 127.0.0.1:${BACKEND_PORT}…"
  (cd "${ROOT}" && exec .venv/bin/python prototype/agent.py serve-all \
    --projects-root prototype/projects --port "${BACKEND_PORT}") &
  BACKEND_PID=$!
  BACKEND_STARTED_BY_US=1
  for _ in $(seq 1 30); do
    probe_backend "${BACKEND_PORT}" && break
    kill -0 "${BACKEND_PID}" 2>/dev/null || fail "backend exited during startup (see output above)"
    sleep 1
  done
  probe_backend "${BACKEND_PORT}" || fail "backend did not become healthy on port ${BACKEND_PORT}"
fi

# -- frontend -----------------------------------------------------------------
HOLDER="$(port_listener "${FRONTEND_PORT}")"
[ -n "${HOLDER}" ] && fail "port ${FRONTEND_PORT} is already in use by PID ${HOLDER} (likely a stale dev server from an earlier run). Stop it first: kill ${HOLDER} — then re-run ./dev.sh"

info "starting frontend on localhost:${FRONTEND_PORT}…"
(cd "${ROOT}/web" && PORT="${FRONTEND_PORT}" exec npm run dev -- --port "${FRONTEND_PORT}") &
FRONTEND_PID=$!

info "ResearchOS is starting:"
info "  frontend  http://localhost:${FRONTEND_PORT}"
info "  backend   http://127.0.0.1:${BACKEND_PORT}/api/health"
info "Press Ctrl-C to stop both."

wait "${FRONTEND_PID}" 2>/dev/null || true

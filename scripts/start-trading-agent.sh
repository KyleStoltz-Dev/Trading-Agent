#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

AUTO_MODE="${AUTO_MODE:-0}"
TRADING_AGENT_BIN="${TRADING_AGENT_BIN:-.venv/bin/trading-agent}"
SESSION_NAME="${TRADING_AGENT_SESSION_NAME:-quick-start}"
OPEN_CHAT="${OPEN_CHAT:-1}"
RUN_QUICKSTART="${RUN_QUICKSTART:-0}"

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/start-trading-agent.sh [--quickstart] [--chat] [--no-chat] [--auto] [--name session-name]

Options:
  --quickstart    Run `trade quickstart` before launching the interactive session.
  --chat          Explicitly start interactive Trading Agent (default if OPEN_CHAT is unset).
  --no-chat       Skip opening chat (useful for smoke-checks).
  --auto          One-command startup with safe defaults: run quickstart and skip interactive prompts.
  --name          Name for --new session (default: quick-start).
  --help          Show this help text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "${1:-}" in
    --quickstart)
      RUN_QUICKSTART=1
      shift
      ;;
    --chat)
      OPEN_CHAT=1
      shift
      ;;
    --no-chat)
      OPEN_CHAT=0
      shift
      ;;
    --auto)
      AUTO_MODE=1
      RUN_QUICKSTART=1
      shift
      ;;
    --name)
      SESSION_NAME="${2:-}"
      if [[ -z "$SESSION_NAME" ]]; then
        echo "Missing --name argument."
        exit 1
      fi
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: ${1}" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -x "$TRADING_AGENT_BIN" ]] && command -v trade >/dev/null 2>&1; then
  TRADING_AGENT_BIN="$(command -v trade)"
fi

if [[ ! -x "$TRADING_AGENT_BIN" ]]; then
  if [[ "$AUTO_MODE" == "1" ]]; then
    echo "Could not find a local Trading Agent executable. Install first, or set TRADING_AGENT_BIN to .venv/bin/trading-agent / path."
  else
    echo "Could not find an executable Trading Agent at: $TRADING_AGENT_BIN"
    echo "Run ./install-trading-agent.command or ./install-trading-agent.sh first, or set TRADING_AGENT_BIN."
  fi
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "No .env found, creating from .env.example..."
  cp .env.example .env
  chmod 600 .env
fi

PYTHON_BIN="${TRADING_AGENT_BIN%/*}/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Could not locate a Python interpreter to read .env."
  exit 1
fi

extract_env() {
  local key="$1"
  "$PYTHON_BIN" - <<'PY' "$key" ".env"
from pathlib import Path
import re
import sys

key = sys.argv[1]
path = Path(sys.argv[2])
value = ""
pattern = re.compile(r"^(?P<k>[A-Za-z_][A-Za-z0-9_]*)=(?P<v>.*)$")

for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = pattern.match(line)
    if match and match.group("k") == key:
        value = match.group("v")

print(value)
PY
}

write_env_var() {
  local key="$1"
  local value="$2"
  "$PYTHON_BIN" - <<'PY' "$key" "$value" ".env"
from pathlib import Path
import re
import sys

key = sys.argv[1]
value = sys.argv[2]
path = Path(sys.argv[3])
pattern = re.compile(rf"^{re.escape(key)}=.*$")
lines = path.read_text(encoding="utf-8").splitlines()
updated = False

for index, raw in enumerate(lines):
    if pattern.match(raw):
        lines[index] = f"{key}={value}"
        updated = True
        break

if not updated:
    lines.append(f"{key}={value}")

path.write_text("\n".join(lines) + "\\n", encoding="utf-8")
PY
}

POSTGRES_PASSWORD="$(extract_env POSTGRES_PASSWORD)"
DATABASE_URL="$(extract_env DATABASE_URL)"

if [[ -z "$POSTGRES_PASSWORD" ]]; then
  if [[ "$AUTO_MODE" == "1" ]]; then
    echo "POSTGRES_PASSWORD is missing from .env."
    echo "Before using --auto, set it once in .env and rerun."
    exit 1
  fi
  echo "POSTGRES_PASSWORD is missing from .env."
  read -r -s -p "Set POSTGRES_PASSWORD: " POSTGRES_PASSWORD
  echo
  if [[ -z "$POSTGRES_PASSWORD" ]]; then
    echo "POSTGRES_PASSWORD cannot be empty."
    exit 1
  fi
  write_env_var "POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"
fi

if [[ -z "$DATABASE_URL" ]]; then
  if [[ "$AUTO_MODE" == "1" ]]; then
    echo "DATABASE_URL is missing from .env."
    echo "Set DATABASE_URL before using --auto (for example: postgresql+psycopg://trading:YOUR_PASSWORD@localhost:5432/trading_agent)."
  else
    echo "DATABASE_URL is missing from .env."
    echo "Set DATABASE_URL (for example: postgresql+psycopg://trading:YOUR_PASSWORD@localhost:5432/trading_agent)."
  fi
  exit 1
fi

if [[ "$AUTO_MODE" == "1" ]] && [[ "$DATABASE_URL" == *'${POSTGRES_PASSWORD}'* ]]; then
  echo "DATABASE_URL still contains an unresolved \${POSTGRES_PASSWORD} placeholder."
  echo "Expand it in .env (or set a full inline password) before using --auto."
  exit 1
fi

if command -v docker >/dev/null 2>&1; then
  HAS_DOCKER_COMPOSE_PLUGIN=0
  HAS_DOCKER_COMPOSE_LEGACY=0
  if docker compose version >/dev/null 2>&1; then
    HAS_DOCKER_COMPOSE_PLUGIN=1
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    HAS_DOCKER_COMPOSE_LEGACY=1
  fi
fi

if [[ "${HAS_DOCKER_COMPOSE_PLUGIN:-0}" -eq 1 ]]; then
  echo "Starting PostgreSQL with Docker Compose..."
  docker compose up -d postgres
elif [[ "${HAS_DOCKER_COMPOSE_LEGACY:-0}" -eq 1 ]]; then
  echo "Starting PostgreSQL with docker-compose..."
  docker-compose up -d postgres
else
  echo "Docker Compose unavailable; assuming PostgreSQL is already running locally."
fi

for attempt in {1..60}; do
  if "$TRADING_AGENT_BIN" health >/dev/null 2>&1; then
    break
  fi
  if [[ $attempt -eq 60 ]]; then
    echo "Timed out waiting for Trading Agent startup checks (typically PostgreSQL)."
    exit 1
  fi
  sleep 2
done

if [[ "$RUN_QUICKSTART" == "1" ]]; then
  "$TRADING_AGENT_BIN" quickstart
fi

if [[ "$OPEN_CHAT" == "1" ]]; then
  "$TRADING_AGENT_BIN" --new --name "$SESSION_NAME"
else
  echo "Done. DB and agent checks passed."
fi

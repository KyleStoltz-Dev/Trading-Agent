#!/usr/bin/env sh
set -eu

RUN_SETUP=true
for argument in "$@"; do
  case "$argument" in
    --no-setup)
      RUN_SETUP=false
      ;;
    --help|-h)
      echo "Usage: ./install-trading-agent.sh [--no-setup]"
      echo "  --no-setup  install the locked environment without starting the wizard"
      exit 0
      ;;
    *)
      echo "Unknown installer option: $argument" >&2
      exit 2
      ;;
  esac
done

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3.12 or newer is required: https://www.python.org/downloads/"
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or newer is required."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "The existing .venv uses Python older than 3.12; replace that environment first." >&2
  exit 1
fi

.venv/bin/python -m pip install --require-hashes --only-binary=:all: \
  --requirement requirements-bootstrap.txt
.venv/bin/uv sync --locked --inexact --extra ai
if [ "$RUN_SETUP" = true ]; then
  .venv/bin/trade setup
else
  echo "Locked Trading Agent environment installed; guided setup was skipped."
fi

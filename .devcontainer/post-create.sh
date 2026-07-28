#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

cd /workspace

if find . -path './.git' -prune -o -path './.venv' -prune -o \
  -type l -print -quit | grep -q .; then
  echo "Refusing secure development: the clone contains a symbolic link." >&2
  exit 1
fi

if find . -path './.git' -prune -o -path './.venv' -prune -o -type f \
  \( -name '.env' -o \( -name '.env.*' ! -name '.env.example' \) \
     -o -name 'auth.json' -o -name '.netrc' -o -name '.npmrc' \
     -o -name '.pypirc' -o -name 'application_default_credentials.json' \
     -o -name 'id_rsa' -o -name 'id_ed25519' \
     -o -name '*.pem' -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' \) \
  -print -quit | grep -q .; then
  echo "Refusing secure development: the clone contains a known credential filename." >&2
  echo "Use a clean independent clone; never mount the primary checkout containing .env." >&2
  exit 1
fi

if [ ! -d .git ] || ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Refusing secure development: /workspace must be an independent Git clone." >&2
  exit 1
fi

cache_directory="$(mktemp -d /tmp/trading-agent-uv-cache.XXXXXX)"
cleanup() {
  rm -rf -- "$cache_directory"
}
trap cleanup EXIT
cp -R /opt/uv-cache/. "$cache_directory/"
UV_CACHE_DIR="$cache_directory" \
  uv sync --locked --offline --extra dev --extra ai --extra metatrader
echo "Trading Agent secure development environment is ready."

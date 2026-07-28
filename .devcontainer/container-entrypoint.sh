#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

install -d -m 750 -o trading-egress -g vscode /run/trading-agent
/usr/local/bin/trading-agent-init-firewall
if [ -L /tmp/trading-agent-codex-home ]; then
  unlink /tmp/trading-agent-codex-home
elif [ -d /tmp/trading-agent-codex-home ]; then
  find /tmp/trading-agent-codex-home -xdev -mindepth 1 -delete
elif [ -e /tmp/trading-agent-codex-home ]; then
  echo "Refusing startup: the ephemeral Codex path is not a directory." >&2
  exit 1
fi
install -d -m 700 -o vscode -g vscode /tmp/trading-agent-codex-home
exec gosu vscode "$@"

#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

if [ "${EUID}" -ne 0 ]; then
  echo "Firewall initialization must run as root." >&2
  exit 1
fi

fail_closed() {
  iptables -P INPUT DROP 2>/dev/null || true
  iptables -P FORWARD DROP 2>/dev/null || true
  iptables -P OUTPUT DROP 2>/dev/null || true
  ip6tables -P INPUT DROP 2>/dev/null || true
  ip6tables -P FORWARD DROP 2>/dev/null || true
  ip6tables -P OUTPUT DROP 2>/dev/null || true
}

on_exit() {
  status=$?
  if [ "$status" -ne 0 ]; then
    fail_closed
  fi
  exit "$status"
}
trap on_exit EXIT

# Establish deny policies before any DNS, ipset, or proxy operation can fail.
fail_closed
iptables -F
iptables -X
ip6tables -F
ip6tables -X
ipset destroy trading-agent-openai 2>/dev/null || true

iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Root may resolve the single upstream during initialization only.
iptables -A OUTPUT -m owner --uid-owner 0 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner 0 -p tcp --dport 53 -j ACCEPT
ipset create trading-agent-openai hash:ip
while IFS= read -r address; do
  if [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    ipset add trading-agent-openai "$address" -exist
  fi
done < <(getent ahostsv4 api.openai.com | awk '{print $1}' | sort -u)

if [ "$(ipset list trading-agent-openai | awk '/Number of entries/ {print $4}')" = "0" ]; then
  echo "The OpenAI API hostname did not resolve to an IPv4 address." >&2
  exit 1
fi

# Rebuild final rules: only the fixed-upstream Responses API proxy can resolve DNS
# and open TLS connections, and only to the resolved OpenAI API addresses.
# Workspace code can reach that proxy over loopback but has no direct route.
proxy_uid="$(id -u trading-egress)"
iptables -F
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "$proxy_uid" -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "$proxy_uid" -p tcp --dport 53 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "$proxy_uid" -p tcp --dport 443 \
  -m set --match-set trading-agent-openai dst -j ACCEPT
iptables -A INPUT -j REJECT --reject-with icmp-admin-prohibited
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited
iptables -A FORWARD -j REJECT --reject-with icmp-admin-prohibited

gosu trading-egress python3 \
  /usr/local/libexec/trading-agent-responses-api-proxy \
  >/tmp/trading-agent-responses-proxy.log 2>&1 &

for _ in $(seq 1 50); do
  if curl --connect-timeout 1 --max-time 2 \
    http://127.0.0.1:3128/v1/responses >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

proxy_status="$(
  curl --silent --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 \
    --request POST --header 'Content-Length: 0' \
    http://127.0.0.1:3128/v1/responses
)"
if [ "$proxy_status" != "503" ]; then
  echo "Responses proxy verification failed before credential configuration." >&2
  exit 1
fi
if ! gosu trading-egress curl --connect-timeout 2 --max-time 5 \
  https://api.openai.com >/dev/null 2>&1; then
  echo "Fixed egress user could not reach api.openai.com." >&2
  exit 1
fi
if gosu vscode curl --connect-timeout 2 --max-time 3 \
  https://api.openai.com >/dev/null 2>&1; then
  echo "Firewall verification failed: workspace Internet access was reachable." >&2
  exit 1
fi

trap - EXIT

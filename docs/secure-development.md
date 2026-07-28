# Secure development container

Trading Agent's normal `/develop` host backend is intentionally development-only. Codex's
`workspace-write` sandbox restricts writes, but it is not a general host filesystem-read
boundary. For a stronger boundary, this repository includes a secure Dev Container adapted
from OpenAI's
[Codex secure devcontainer reference](https://github.com/openai/codex/tree/main/.devcontainer).

The container:

- mounts only the selected independent Git clone;
- rejects symbolic links and a conservative set of known credential filenames before setup;
- installs dependencies from `uv.lock` and Codex from an npm lock;
- retains Codex's inner Linux sandbox;
- starts fail-closed network rules before repository lifecycle code;
- sends model requests through a fixed-upstream loopback proxy that accepts only
  `POST /v1/responses`, injects the API key outside the Codex process, and connects only to
  `https://api.openai.com/v1/responses`;
- forces stateless responses, allows only the reviewed `gpt-5.6-sol`/`gpt-5.6-terra`
  models and local function/custom/shell tools, rejects remote MCP/search/computer tools,
  and caps output, request rate, and concurrency;
- denies direct workspace DNS/Internet access and default-denies IPv6;
- drops access to the host PostgreSQL socket, home directory, Git configuration, and primary
  checkout;
- keeps Codex state ephemeral and removes `sudo` from the development user; and
- limits the container process count.

This is substantial isolation, not a guarantee against every malicious repository. Code in
the mounted clone can submit allowed text to OpenAI through the loopback model proxy and
consume the configured API budget, although it does not receive the credential and cannot
request server-side remote tools or stored/background work. It can also read every file in
the clone. Use an isolated clone, a dedicated API project/key with a low spend limit, and
review the resulting diff before approval.

The outer container grants `SYS_ADMIN`, `SYS_CHROOT`, `SETUID`, `SETGID`, `NET_ADMIN`, and
`NET_RAW`, and disables its seccomp/AppArmor profiles so Codex can create the inner Bubblewrap
sandbox and the entrypoint can establish egress controls. Those relaxations increase the
outer-container attack surface. Keep Docker fully patched. Use a disposable VM instead when
the repository itself may be malicious; this profile is for containing agent-generated work
inside a repository you already trust.

## Prerequisites

Install Docker Desktop or another Dev Container-compatible runtime. Allocate at least 4 GiB
of memory and two CPUs to Docker. Install the reviewed Dev Container CLI version:

```bash
npm install --global @devcontainers/cli@0.88.0
```

Docker must be running. Do not start the secure profile from the primary checkout because it
normally contains the private `.env`.

## Start from an isolated clone

```bash
mkdir -p .data/development/clones
git clone --no-hardlinks . .data/development/clones/manual-secure
cd .data/development/clones/manual-secure
git remote remove origin
git switch -c agent/manual-secure
devcontainer up \
  --workspace-folder . \
  --config .devcontainer/devcontainer.secure.json
```

Configure the fixed-upstream proxy once after each container start. The helper reads the key
without echoing it and passes it through a mode-restricted Unix socket; the key is locked in
the separate proxy process where supported, while core dumps and process inspection are
disabled. It is never placed in the Codex process environment:

```bash
read -r -s -p "Dedicated Codex API key: " codex_proxy_key
printf "\n"
printf '%s\n' "$codex_proxy_key" | devcontainer exec \
  --workspace-folder . \
  --config .devcontainer/devcontainer.secure.json \
  trading-agent-configure-codex-proxy --stdin
unset codex_proxy_key
```

Run one bounded task through that proxy:

```bash
devcontainer exec \
  --workspace-folder . \
  --config .devcontainer/devcontainer.secure.json \
  codex exec \
    -c 'model_providers.trading-agent-proxy={ name="Trading Agent Proxy", base_url="http://127.0.0.1:3128/v1", wire_api="responses" }' \
    -c 'model_provider="trading-agent-proxy"' \
    --model gpt-5.6-sol \
    --sandbox workspace-write --ephemeral \
    --ignore-user-config \
    "Implement the confirmed change, run focused tests, and leave the diff for review."
```

Use `git diff`, the Trading Agent static scans, and the normal PR review process before
committing. Never mount the primary `.env`, broker credentials, database dumps, or private
journal exports into the container.

On Windows, run the same commands from WSL2 so file permissions, Bash, Docker paths, and the
credential prompt behave consistently. PowerShell-only startup is not currently a verified
path.

## Verification

The entrypoint fails closed if DNS, firewall, or proxy initialization fails. Only the
unprivileged fixed-upstream proxy process may resolve DNS and open TLS to the current OpenAI
API address set; the workspace user has no direct route. The proxy rejects every method/path
except `POST /v1/responses` and overrides the upstream authorization and host itself. Restart
the container to refresh the upstream address set after a long network interruption or CDN
address change.

The container cannot be runtime-verified on a machine without Docker. CI builds and starts
the profile, checks Git detection and credential non-persistence, and exercises its allowed
and denied network paths. A passing container test still does not turn an untrusted repository
into trusted code.

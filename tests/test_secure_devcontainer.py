import http.client
import json
import re
import runpy
import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEVCONTAINER = ROOT / ".devcontainer"


def test_secure_devcontainer_is_bounded_and_does_not_mount_host_secrets() -> None:
    config = json.loads((DEVCONTAINER / "devcontainer.secure.json").read_text(encoding="utf-8"))

    assert config["workspaceFolder"] == "/workspace"
    assert "${localEnv:HOME}" not in json.dumps(config)
    assert "CODEX_API_KEY" not in json.dumps(config)
    assert config["containerEnv"]["CODEX_HOME"].startswith("/tmp/")
    assert "HTTP_PROXY" not in json.dumps(config)
    assert "mounts" not in config
    assert "--pids-limit=512" in config["runArgs"]
    assert config["overrideCommand"] is False
    assert "postStartCommand" not in config


def test_secure_devcontainer_uses_locked_codex_cli_and_python_dependencies() -> None:
    package = json.loads(
        (DEVCONTAINER / "codex-install" / "package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (DEVCONTAINER / "codex-install" / "package-lock.json").read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (DEVCONTAINER / "Dockerfile.secure").read_text(encoding="utf-8")
    post_create = (DEVCONTAINER / "post-create.sh").read_text(encoding="utf-8")

    assert package["dependencies"]["@openai/codex"] == "0.121.0"
    assert lock["packages"][""]["dependencies"]["@openai/codex"] == "0.121.0"
    assert "npm ci --omit=dev" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.11.32" in dockerfile
    assert len(re.findall(r"@sha256:[0-9a-f]{64}", dockerfile)) == 2
    assert "curl -fsSL" not in dockerfile
    assert "rm -f /usr/bin/sudo" in dockerfile
    assert "--group release" in dockerfile
    assert "editables==0.5" in pyproject["dependency-groups"]["release"]
    assert 'ENTRYPOINT ["/usr/local/bin/trading-agent-container-entrypoint"]' in dockerfile
    assert "uv sync --locked --offline" in post_create
    assert "--no-install-project" in post_create
    assert "--no-build-isolation" in post_create
    assert "--group release" in post_create
    assert "! -name '.env.example'" in post_create

    instructions = (ROOT / "docs" / "secure-development.md").read_text(encoding="utf-8")
    assert "git clone --no-hardlinks" in instructions
    assert "git worktree add" not in instructions


def test_secure_devcontainer_shell_sources_parse() -> None:
    for script in ("container-entrypoint.sh", "init-firewall.sh", "post-create.sh"):
        result = subprocess.run(
            ["bash", "-n", str(DEVCONTAINER / script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    for script in ("configure-codex-proxy.py", "responses-api-proxy.py"):
        path = DEVCONTAINER / script
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_secure_devcontainer_network_is_initialized_fail_closed() -> None:
    firewall = (DEVCONTAINER / "init-firewall.sh").read_text(encoding="utf-8")
    proxy = (DEVCONTAINER / "responses-api-proxy.py").read_text(encoding="utf-8")

    assert firewall.index("fail_closed") < firewall.index("getent ahostsv4")
    assert "trap on_exit EXIT" in firewall
    assert '--uid-owner "$proxy_uid"' in firewall
    assert "id -u trading-egress" in firewall
    assert 'UPSTREAM_HOST = "api.openai.com"' in proxy
    assert 'UPSTREAM_PATH = "/v1/responses"' in proxy
    assert "if self.path != UPSTREAM_PATH" in proxy
    assert 'headers["Authorization"] = authorization' in proxy
    assert 'headers["Host"] = UPSTREAM_HOST' in proxy
    assert "MAX_REQUESTS_PER_MINUTE = 60" in proxy
    assert "MAX_CONCURRENT_REQUESTS = 2" in proxy
    assert "MAX_OUTPUT_TOKENS = 32_768" in proxy
    assert "connection.settimeout(5)" in proxy
    assert "response.read1(64 * 1024)" in proxy
    assert ".mlock(" in proxy
    assert "resource.RLIMIT_CORE" in proxy
    assert ".prctl(4, 0, 0, 0, 0)" in proxy


def test_responses_proxy_rejects_everything_until_configured() -> None:
    module = runpy.run_path(str(DEVCONTAINER / "responses-api-proxy.py"))
    try:
        server = module["BoundedThreadingHTTPServer"](
            ("127.0.0.1", 0),
            module["ResponsesHandler"],
        )
    except PermissionError:
        pytest.skip("the execution sandbox does not permit loopback listeners")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/v1/responses")
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("POST", "/not-allowed", body=b"")
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("POST", "/v1/responses", body=b"")
        assert connection.getresponse().status == 503
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_responses_proxy_forces_stateless_local_tool_policy() -> None:
    module = runpy.run_path(str(DEVCONTAINER / "responses-api-proxy.py"))
    policy = module["request_policy"]
    payload = json.loads(
        policy(
            json.dumps(
                {
                    "model": "gpt-5.6-sol",
                    "input": "Review this diff.",
                    "store": True,
                    "tool_choice": {"type": "function", "name": "read_file"},
                    "tools": [
                        {"type": "function", "name": "read_file"},
                        {"type": "custom", "name": "apply_patch"},
                        {"type": "local_shell"},
                    ],
                }
            ).encode()
        )
    )

    assert payload["store"] is False
    assert payload["background"] is False
    assert payload["max_output_tokens"] == 32_768


def test_responses_proxy_allows_small_inline_images_without_remote_fetching() -> None:
    module = runpy.run_path(str(DEVCONTAINER / "responses-api-proxy.py"))
    policy = module["request_policy"]
    payload = json.loads(
        policy(
            json.dumps(
                {
                    "model": "gpt-5.6-sol",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": (
                                        "data:image/png;base64,"
                                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
                                        "CAQAAAC1HAwCAAAAC0lEQVR42mNk"
                                        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                                    ),
                                }
                            ],
                        }
                    ],
                }
            ).encode()
        )
    )

    assert payload["input"][0]["content"][0]["type"] == "input_image"


@pytest.mark.parametrize(
    "override",
    [
        {"model": "unapproved-model"},
        {"background": True},
        {"previous_response_id": "response-from-another-run"},
        {"conversation": "stored-conversation"},
        {"prompt": {"id": "stored-prompt"}},
        {"tools": [{"type": "mcp", "server_url": "https://attacker.invalid"}]},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "computer_use"}]},
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "https://attacker.invalid/private.png",
                        }
                    ],
                }
            ]
        },
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_url": "https://attacker.invalid/private.pdf",
                        }
                    ],
                }
            ]
        },
        {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file_stored"}],
                }
            ]
        },
        {
            "tool_choice": {
                "type": "allowed_tools",
                "tools": [{"type": "web_search"}],
            }
        },
        {"tool_choice": {"type": "function", "name": "undeclared"}},
        {"max_output_tokens": 32_769},
    ],
)
def test_responses_proxy_rejects_server_side_or_unbounded_requests(
    override: dict[str, object],
) -> None:
    module = runpy.run_path(str(DEVCONTAINER / "responses-api-proxy.py"))
    policy = module["request_policy"]
    request = {
        "model": "gpt-5.6-sol",
        "input": "Review this diff.",
        **override,
    }

    with pytest.raises(ValueError):
        policy(json.dumps(request).encode())

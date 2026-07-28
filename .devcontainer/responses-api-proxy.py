#!/usr/bin/env python3
"""Fixed-upstream loopback proxy for the OpenAI Responses API.

The unprivileged coding user can call this service but cannot read its key or create
outbound sockets. Only POST /v1/responses is forwarded to the fixed TLS upstream.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import grp
import http.client
import http.server
import json
import os
import re
import resource
import socket
import socketserver
import ssl
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 3128
CONTROL_SOCKET = Path("/run/trading-agent/codex-proxy.sock")
UPSTREAM_HOST = "api.openai.com"
UPSTREAM_PATH = "/v1/responses"
MAX_KEY_BYTES = 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_TOKENS = 32_768
MAX_REQUESTS_PER_MINUTE = 60
MAX_CONCURRENT_REQUESTS = 2
KEY_PATTERN = re.compile(rb"^[A-Za-z0-9_-]{16,1024}$")
ALLOWED_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6-terra"})
ALLOWED_TOOL_TYPES = frozenset({"custom", "function", "local_shell"})
ALLOWED_TOOL_CHOICES = frozenset({"auto", "none", "required"})
MAX_INLINE_IMAGE_BYTES = 3 * 1024 * 1024
DATA_IMAGE_PATTERN = re.compile(
    r"^data:image/(?:jpeg|png|webp);base64,(?P<data>[A-Za-z0-9+/]*={0,2})$"
)
REMOTE_REFERENCE_FIELDS = frozenset({"file_id", "image_file_id", "vector_store_id"})
BLOCKED_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
BLOCKED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "proxy-authenticate",
        "set-cookie",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class KeyState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._authorization: bytearray | None = None

    def configure_once(self, value: bytearray) -> bool:
        if not KEY_PATTERN.fullmatch(value):
            return False
        with self._lock:
            if self._authorization is not None:
                return False
            authorization = bytearray(b"Bearer ")
            authorization.extend(value)
            address = ctypes.addressof(ctypes.c_char.from_buffer(authorization))
            if (
                ctypes.CDLL(None, use_errno=True).mlock(
                    ctypes.c_void_p(address),
                    ctypes.c_size_t(len(authorization)),
                )
                != 0
            ):
                for index in range(len(authorization)):
                    authorization[index] = 0
                return False
            self._authorization = authorization
        return True

    def authorization(self) -> bytes | None:
        with self._lock:
            return bytes(self._authorization) if self._authorization is not None else None


KEY_STATE = KeyState()


class RequestGate:
    def __init__(self) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._lock = threading.Lock()
        self._started: deque[float] = deque()

    def acquire(self) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        now = time.monotonic()
        with self._lock:
            while self._started and self._started[0] <= now - 60:
                self._started.popleft()
            if len(self._started) >= MAX_REQUESTS_PER_MINUTE:
                self._slots.release()
                return False
            self._started.append(now)
        return True

    def release(self) -> None:
        self._slots.release()


REQUEST_GATE = RequestGate()


class BoundedThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 16


class ResponsesHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TradingAgentResponsesProxy/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != UPSTREAM_PATH:
            self._error(403, b"forbidden\n")
            return
        authorization = KEY_STATE.authorization()
        if authorization is None:
            self._error(503, b"proxy credential is not configured\n")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(400, b"chunked request bodies are not accepted\n")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(400, b"valid content length required\n")
            return
        if not 0 <= length <= MAX_REQUEST_BYTES:
            self._error(413, b"request body exceeds configured limit\n")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._error(400, b"incomplete request body\n")
            return
        try:
            body = request_policy(body)
        except ValueError:
            self._error(403, b"request is outside the secure proxy policy\n")
            return
        if not REQUEST_GATE.acquire():
            self._error(429, b"proxy request limit reached\n")
            return
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.casefold() not in BLOCKED_REQUEST_HEADERS
        }
        headers["Authorization"] = authorization
        headers["Host"] = UPSTREAM_HOST
        headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPSConnection(
            UPSTREAM_HOST,
            443,
            timeout=300,
            context=ssl.create_default_context(),
        )
        try:
            connection.request("POST", UPSTREAM_PATH, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.casefold() not in BLOCKED_RESPONSE_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read1(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ssl.SSLError):
            if not self.wfile.closed:
                self.close_connection = True
        finally:
            connection.close()
            REQUEST_GATE.release()

    def do_GET(self) -> None:  # noqa: N802
        self._error(403, b"forbidden\n")

    def _error(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, _format: str, *args: object) -> None:
        return


def request_policy(body: bytes) -> bytes:
    try:
        payload: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    if payload.get("model") not in ALLOWED_MODELS:
        raise ValueError("model is not allowed")
    if payload.get("background") not in (None, False):
        raise ValueError("background responses are not allowed")
    if payload.get("previous_response_id") is not None:
        raise ValueError("stored response continuation is not allowed")
    if payload.get("conversation") is not None or payload.get("prompt") is not None:
        raise ValueError("server-side stored context is not allowed")
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("tool list is invalid")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in ALLOWED_TOOL_TYPES:
            raise ValueError("server-side tool is not allowed")
    _validate_tool_choice(payload.get("tool_choice"), tools)
    _validate_input_references(payload.get("input"))
    output_tokens = payload.get("max_output_tokens", MAX_OUTPUT_TOKENS)
    if (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or not 1 <= output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ValueError("output token limit is invalid")
    payload["max_output_tokens"] = output_tokens
    payload["store"] = False
    payload["background"] = False
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def _validate_tool_choice(value: Any, tools: list[dict[str, Any]]) -> None:
    if isinstance(value, str):
        if value not in ALLOWED_TOOL_CHOICES:
            raise ValueError("tool choice is not allowed")
        return
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("tool choice is not allowed")
    tool_type = value.get("type")
    allowed_keys = {"type"} if tool_type == "local_shell" else {"type", "name"}
    if tool_type not in ALLOWED_TOOL_TYPES or set(value) != allowed_keys:
        raise ValueError("tool choice is not allowed")
    if tool_type != "local_shell" and (not isinstance(value.get("name"), str) or not value["name"]):
        raise ValueError("tool choice is not allowed")
    if not any(
        tool.get("type") == tool_type
        and (tool_type == "local_shell" or tool.get("name") == value.get("name"))
        for tool in tools
    ):
        raise ValueError("tool choice must name a declared local tool")


def _validate_input_references(value: Any, *, field: str | None = None) -> None:
    if field in REMOTE_REFERENCE_FIELDS and value is not None:
        raise ValueError("stored file references are not allowed")
    if field is not None and (field == "url" or field.endswith("_url")):
        if not isinstance(value, str):
            raise ValueError("input URL field is invalid")
        match = DATA_IMAGE_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("remote input URLs are not allowed")
        try:
            decoded = base64.b64decode(match.group("data"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("inline image is invalid") from exc
        if len(decoded) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError("inline image exceeds the configured limit")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_input_references(item, field=str(key).casefold())
    elif isinstance(value, list):
        for item in value:
            _validate_input_references(item)


def configure_key() -> None:
    CONTROL_SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    CONTROL_SOCKET.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(CONTROL_SOCKET))
        os.chown(CONTROL_SOCKET, -1, grp.getgrnam("vscode").gr_gid)
        CONTROL_SOCKET.chmod(0o620)
        listener.listen(1)
        while KEY_STATE.authorization() is None:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                value = bytearray()
                try:
                    while len(value) <= MAX_KEY_BYTES:
                        chunk = connection.recv(min(256, MAX_KEY_BYTES + 1 - len(value)))
                        if not chunk:
                            break
                        value.extend(chunk)
                        if b"\n" in value:
                            del value[value.index(b"\n") :]
                            break
                except TimeoutError:
                    pass
                accepted = KEY_STATE.configure_once(value)
                for index in range(len(value)):
                    value[index] = 0
                try:
                    connection.sendall(b"ok\n" if accepted else b"rejected\n")
                except BrokenPipeError:
                    pass
    CONTROL_SOCKET.unlink(missing_ok=True)


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError("could not disable process dumpability")
    os.umask(0o077)
    threading.Thread(target=configure_key, name="credential-control", daemon=True).start()
    with BoundedThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ResponsesHandler) as server:
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()

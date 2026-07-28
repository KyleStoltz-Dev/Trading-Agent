#!/usr/bin/env python3
"""Prompt for a dedicated key and pass it once to the privileged local proxy."""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
from pathlib import Path

CONTROL_SOCKET = Path("/run/trading-agent/codex-proxy.sock")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read one key line from stdin instead of using the container terminal.",
    )
    arguments = parser.parse_args()
    if arguments.stdin:
        key = bytearray(sys.stdin.buffer.readline(1026).removesuffix(b"\n"))
    else:
        key = bytearray(getpass.getpass("Dedicated Codex API key: ").encode("ascii"))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(CONTROL_SOCKET))
            client.sendall(key + b"\n")
            result = client.recv(32)
        if result != b"ok\n":
            raise SystemExit("The key was rejected or this container was already configured.")
    finally:
        for index in range(len(key)):
            key[index] = 0
    print("Secure Responses API proxy configured for this container.")


if __name__ == "__main__":
    main()

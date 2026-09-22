"""Private local EA input files; never broker passwords or model-accessible tools."""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path


def private_bytes(path: Path, *, limit: int = 8192) -> bytes:
    """Open a regular, current-user-only file without following the final symlink."""
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("Pairing path must not contain symlinks")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid pairing file")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Pairing must be owned by you and private (mode 0600)")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Pairing file is too large")
        return data
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Pairing:
    account: str
    server: str
    symbol: str
    port: int
    token: str = field(repr=False)


def read_pairing(path: Path) -> Pairing:
    raw = private_bytes(path)
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    values = {}
    allowed = {"ExpectedAccount", "ExpectedServer", "QuoteSymbol", "ReceiverPort", "ReceiverToken"}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in allowed or key in values:
            raise ValueError("Unrecognized or duplicate pairing field")
        # MT5 exports may append optimization metadata to numeric inputs.
        values[key] = value.split("||", 1)[0]
    if set(values) != allowed:
        raise ValueError("Pairing is incomplete")
    account, server, symbol = (
        values[k] for k in ("ExpectedAccount", "ExpectedServer", "QuoteSymbol")
    )
    token = values["ReceiverToken"]
    port = int(values["ReceiverPort"])
    if not re.fullmatch(r"[0-9]{1,20}", account) or int(account) == 0:
        raise ValueError("Invalid account")
    if not server or len(server) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in server):
        raise ValueError("Invalid server")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", symbol):
        raise ValueError("Invalid symbol")
    if not 1024 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Invalid receiver port or token")
    return Pairing(account, server, symbol, port, token)

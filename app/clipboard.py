import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_CLIPBOARD_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class ClipboardImageError(RuntimeError):
    pass


class ClipboardDependencyError(ClipboardImageError):
    pass


class ClipboardImageNotFoundError(ClipboardImageError):
    pass


@dataclass(frozen=True)
class ClipboardImage:
    data: bytes
    content_type: str
    source: str


def detect_image_content_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ClipboardImageError("clipboard image staging path became a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ClipboardImageError("clipboard image staging output is not a regular file")
        if file_stat.st_size > max_bytes:
            raise ClipboardImageError("clipboard image exceeds 10 MB")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ClipboardImageError("clipboard image exceeds 10 MB")
        return data
    finally:
        os.close(descriptor)


def _run_command_to_path(
    command: list[str],
    output_path: Path,
    *,
    command_writes_output: bool = False,
    max_bytes: int,
    timeout_seconds: float = 10,
) -> tuple[bool, bytes]:
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_handle = None
    try:
        if not command_writes_output:
            output_handle = output_path.open("xb")
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if command_writes_output else output_handle,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if output_path.exists() and output_path.stat().st_size > max_bytes:
                process.kill()
                process.wait()
                raise ClipboardImageError("clipboard image exceeds 10 MB")
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise ClipboardImageError("clipboard image read timed out")
            time.sleep(0.02)
        if process.returncode != 0:
            return False, b""
    except OSError as exc:
        raise ClipboardImageError(f"clipboard image reader failed: {exc}") from exc
    finally:
        if output_handle is not None:
            output_handle.close()
    return True, _read_bounded_regular_file(output_path, max_bytes)


def _validated_image(data: bytes, *, source: str) -> ClipboardImage:
    content_type = detect_image_content_type(data)
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ClipboardImageNotFoundError(
            "The clipboard does not contain a PNG, JPEG, or WebP image. "
            "Copy the chart image itself, then try again."
        )
    return ClipboardImage(data=data, content_type=content_type, source=source)


def _read_macos_clipboard(directory: Path, max_bytes: int) -> ClipboardImage:
    osascript = shutil.which("osascript")
    if osascript is None:
        raise ClipboardDependencyError(
            "macOS clipboard image support requires the built-in `osascript` command."
        )
    output = directory / "clipboard.png"
    script = """
on run argv
    set outputPath to item 1 of argv
    try
        set imageData to the clipboard as «class PNGf»
    on error
        error number 2
    end try
    set outputFile to open for access (POSIX file outputPath) with write permission
    try
        set eof outputFile to 0
        write imageData to outputFile
        close access outputFile
    on error
        try
            close access outputFile
        end try
        error number 3
    end try
end run
""".strip()
    ok, data = _run_command_to_path(
        [osascript, "-e", script, str(output)],
        output,
        command_writes_output=True,
        max_bytes=max_bytes,
    )
    if not ok:
        raise ClipboardImageNotFoundError(
            "The clipboard does not contain a copyable chart image. "
            "Copy the image itself in Preview, TradingView, or your browser, then try again."
        )
    return _validated_image(data, source="macOS clipboard")


def _read_windows_clipboard(directory: Path, max_bytes: int) -> ClipboardImage:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise ClipboardDependencyError(
            "Windows clipboard image support requires PowerShell."
        )
    output = directory / "clipboard.png"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "if (-not [Windows.Forms.Clipboard]::ContainsImage()) { exit 2 };"
        "$image=[Windows.Forms.Clipboard]::GetImage();"
        "$image.Save($args[0],[Drawing.Imaging.ImageFormat]::Png);"
        "$image.Dispose()"
    )
    ok, data = _run_command_to_path(
        [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", script, str(output)],
        output,
        command_writes_output=True,
        max_bytes=max_bytes,
    )
    if not ok:
        raise ClipboardImageNotFoundError(
            "The Windows clipboard does not contain an image. Copy the chart image, then try again."
        )
    return _validated_image(data, source="Windows clipboard")


def _read_linux_clipboard(directory: Path, max_bytes: int) -> ClipboardImage:
    wl_paste = shutil.which("wl-paste")
    xclip = shutil.which("xclip")
    if wl_paste is None and xclip is None:
        raise ClipboardDependencyError(
            "Linux clipboard image support requires `wl-paste` (wl-clipboard) "
            "or `xclip`. Install the one used by your desktop session."
        )
    attempts: list[tuple[list[str], str, str]] = []
    for index, content_type in enumerate(("image/png", "image/jpeg", "image/webp"), start=1):
        if wl_paste is not None:
            attempts.append(
                (
                    [wl_paste, "--no-newline", "--type", content_type],
                    content_type,
                    f"wayland-{index}",
                )
            )
        if xclip is not None:
            attempts.append(
                (
                    [xclip, "-selection", "clipboard", "-t", content_type, "-o"],
                    content_type,
                    f"x11-{index}",
                )
            )
    for command, requested_type, name in attempts:
        output = directory / f"{name}.img"
        ok, data = _run_command_to_path(
            command,
            output,
            max_bytes=max_bytes,
        )
        if not ok:
            continue
        image = _validated_image(data, source="Linux clipboard")
        if image.content_type != requested_type:
            raise ClipboardImageError(
                "clipboard image type did not match the requested PNG, JPEG, or WebP format"
            )
        return image
    raise ClipboardImageNotFoundError(
        "The Linux clipboard does not contain a PNG, JPEG, or WebP image. "
        "Copy the chart image itself, then try again."
    )


def read_clipboard_image(
    *,
    max_bytes: int = MAX_CLIPBOARD_IMAGE_BYTES,
) -> ClipboardImage:
    if max_bytes <= 0:
        raise ValueError("clipboard image byte limit must be positive")
    with tempfile.TemporaryDirectory(prefix="trading-agent-clipboard-") as temporary:
        directory = Path(temporary)
        system = platform.system().lower()
        if system == "darwin":
            return _read_macos_clipboard(directory, max_bytes)
        if system == "windows":
            return _read_windows_clipboard(directory, max_bytes)
        if system == "linux":
            return _read_linux_clipboard(directory, max_bytes)
        raise ClipboardDependencyError(
            f"Clipboard image capture is not supported on {platform.system() or 'this platform'}. "
            "Save the chart as PNG, JPEG, or WebP and pass its path instead."
        )

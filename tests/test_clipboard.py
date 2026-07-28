from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import app.cli as cli_module
import app.clipboard as clipboard_module
from app.cli import app
from app.clipboard import (
    ClipboardDependencyError,
    ClipboardImage,
    ClipboardImageError,
    ClipboardImageNotFoundError,
    _read_bounded_regular_file,
    detect_image_content_type,
    read_clipboard_image,
)
from app.config import Settings

runner = CliRunner()
PNG = b"\x89PNG\r\n\x1a\nchart"
JPEG = b"\xff\xd8\xffchart"
WEBP = b"RIFF\x08\x00\x00\x00WEBPchart"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
        (b"/Users/trader/private-chart.png", None),
        (b"clipboard text", None),
    ],
)
def test_clipboard_image_type_is_detected_from_bytes(
    data: bytes,
    expected: str | None,
) -> None:
    assert detect_image_content_type(data) == expected


def test_linux_clipboard_requests_only_supported_image_mime_types(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(clipboard_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        clipboard_module.shutil,
        "which",
        lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None,
    )

    def fake_run(
        command: list[str],
        _output: Path,
        *,
        command_writes_output: bool = False,
        max_bytes: int,
        timeout_seconds: float = 10,
    ) -> tuple[bool, bytes]:
        del command_writes_output, max_bytes, timeout_seconds
        commands.append(command)
        return True, PNG

    monkeypatch.setattr(clipboard_module, "_run_command_to_path", fake_run)

    image = read_clipboard_image()

    assert image.data == PNG
    assert image.content_type == "image/png"
    assert commands == [["/usr/bin/wl-paste", "--no-newline", "--type", "image/png"]]
    assert all("text" not in part.casefold() for part in commands[0])


def test_linux_clipboard_never_interprets_text_as_an_image_path(monkeypatch) -> None:
    monkeypatch.setattr(clipboard_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        clipboard_module.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )
    monkeypatch.setattr(
        clipboard_module,
        "_run_command_to_path",
        lambda *_args, **_kwargs: (True, b"/Users/trader/chart.png"),
    )

    with pytest.raises(ClipboardImageNotFoundError, match="does not contain"):
        read_clipboard_image()


def test_clipboard_dependency_errors_are_platform_specific(monkeypatch) -> None:
    monkeypatch.setattr(clipboard_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clipboard_module.shutil, "which", lambda _name: None)

    with pytest.raises(ClipboardDependencyError, match="wl-paste.*xclip"):
        read_clipboard_image()


def test_clipboard_staging_enforces_the_byte_limit(tmp_path: Path) -> None:
    staged = tmp_path / "clipboard.png"
    staged.write_bytes(PNG)

    with pytest.raises(ClipboardImageError, match="exceeds 10 MB"):
        _read_bounded_regular_file(staged, max_bytes=len(PNG) - 1)


def test_clipboard_reader_rejects_type_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(clipboard_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        clipboard_module.shutil,
        "which",
        lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None,
    )
    monkeypatch.setattr(
        clipboard_module,
        "_run_command_to_path",
        lambda *_args, **_kwargs: (True, JPEG),
    )

    with pytest.raises(ClipboardImageError, match="did not match"):
        read_clipboard_image()


def test_chart_requires_exactly_one_path_or_clipboard_source(monkeypatch) -> None:
    authorize = Mock()
    monkeypatch.setattr(cli_module, "_authorize_direct", authorize)

    result = runner.invoke(app, ["chart"])

    assert result.exit_code == 2
    assert "exactly one chart source" in result.stdout
    authorize.assert_not_called()


def test_chart_clipboard_disclosure_contains_bytes_but_never_a_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    provider = SimpleNamespace(name="openai", model="vision-model")
    authorize = Mock()
    path_reader = Mock(side_effect=AssertionError("path reader must not be used"))
    confirmation = Mock(return_value=False)
    monkeypatch.setattr(cli_module, "_authorize_direct", authorize)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: Settings(
            openai_api_key="test-key",
            evidence_directory=tmp_path / "evidence",
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "read_clipboard_image",
        Mock(
            return_value=ClipboardImage(
                data=PNG,
                content_type="image/png",
                source="system clipboard",
            )
        ),
    )
    monkeypatch.setattr(cli_module, "_read_approved_chart", path_reader)
    monkeypatch.setattr(cli_module, "create_model_provider", lambda _settings: provider)
    monkeypatch.setattr(cli_module, "_chart_destination", lambda *_args: "https://api.openai.com")
    monkeypatch.setattr(cli_module, "_confirm_agent_external_action", confirmation)

    result = runner.invoke(app, ["chart", "--clipboard", "--context", "Pre-trade review"])

    assert result.exit_code == 1
    path_reader.assert_not_called()
    action, disclosure = confirmation.call_args.args
    assert action == "External disclosure: hosted chart analysis"
    assert disclosure == {
        "provider": "openai",
        "destination": "https://api.openai.com",
        "image_source": "system clipboard",
        "content_type": "image/png",
        "image_bytes": len(PNG),
        "context": "Pre-trade review",
    }
    assert "image_path" not in disclosure

from pathlib import Path

import pytest

from app.setup import (
    dependency_guidance,
    estimate_ollama_download_size,
    install_user_launcher,
    launcher_target_for_interpreter,
    ollama_profile_settings,
    provider_settings,
    pull_ollama_model,
    start_local_service,
    update_env_file,
)
from app.system_resources import GIB, ResourceSnapshot


def test_setup_normalizes_duplicate_provider_without_touching_secrets(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODEL_PROVIDER=openai\n"
        "OPENAI_API_KEY=keep-this-value\n"
        "MODEL_PROVIDER=anthropic\n"
        "OLLAMA_MODEL=old\n",
        encoding="utf-8",
    )

    update_env_file(env_file, provider_settings("ollama", "qwen3.5:9b"))

    content = env_file.read_text(encoding="utf-8")
    assert content.count("MODEL_PROVIDER=") == 1
    assert "MODEL_PROVIDER=ollama" in content
    assert "OPENAI_API_KEY=keep-this-value" in content
    assert "OLLAMA_MODEL=qwen3.5:9b" in content
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_setup_refuses_to_write_secret_settings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        update_env_file(tmp_path / ".env", {"OPENAI_API_KEY": "secret"})


def test_setup_rejects_environment_injection(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="control characters"):
        update_env_file(
            tmp_path / ".env",
            {"MODEL_PROVIDER": "ollama\nOPENAI_API_KEY=injected"},
        )

    with pytest.raises(ValueError, match="model name"):
        provider_settings("ollama", "qwen3.5:9b\nOPENAI_API_KEY=injected")


def test_ollama_quality_profile_changes_balanced_and_deep_only() -> None:
    assert ollama_profile_settings("qwen3.5:35b-a3b", "quality") == {
        "OLLAMA_BALANCED_MODEL": "qwen3.5:35b-a3b",
        "OLLAMA_DEEP_MODEL": "qwen3.5:35b-a3b",
    }


def test_ollama_download_estimate_is_conservative_and_tag_driven() -> None:
    assert estimate_ollama_download_size("qwen3.5:9b") == int(9.2 * GIB)
    assert estimate_ollama_download_size("qwen3.5:35b-a3b") == 30 * GIB
    assert estimate_ollama_download_size("model:latest") is None


def test_ollama_pull_blocks_before_subprocess_when_disk_is_insufficient(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("app.setup.shutil.which", lambda _name: "/usr/bin/ollama")
    monkeypatch.setattr(
        "app.setup.resource_snapshot",
        lambda _path: ResourceSnapshot(
            platform="TestOS",
            total_memory_bytes=64 * GIB,
            available_memory_bytes=60 * GIB,
            memory_percent=5,
            swap_total_bytes=0,
            swap_used_bytes=0,
            swap_percent=0,
            disk_free_bytes=5 * GIB,
        ),
    )
    monkeypatch.setattr(
        "app.setup.subprocess.run",
        lambda command, **_kwargs: calls.append(command),
    )

    pulled, detail = pull_ollama_model("qwen3.5:35b-a3b")

    assert pulled is False
    assert "blocked before network transfer" in detail
    assert calls == []


def test_ollama_pull_requires_explicit_size_for_ambiguous_tag(monkeypatch) -> None:
    monkeypatch.setattr("app.setup.shutil.which", lambda _name: "/usr/bin/ollama")

    pulled, detail = pull_ollama_model("model:latest")

    assert pulled is False
    assert "--expected-size-gb" in detail


def test_launcher_refuses_to_replace_regular_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "venv" / "bin" / "trade"
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    home = tmp_path / "home"
    launcher = home / ".local" / "bin" / "trade"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("user file", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(FileExistsError, match="refusing"):
        install_user_launcher(target)


def test_launcher_creates_absolute_symlink(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "venv" / "bin" / "trade"
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    launcher = install_user_launcher(target)

    assert launcher.is_symlink()
    assert launcher.resolve() == target.resolve()


def test_windows_launcher_is_command_file_not_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "venv" / "Scripts" / "trade.exe"
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    launcher = install_user_launcher(target, system="windows")

    assert launcher == local_app_data / "TradingAgent" / "bin" / "trade.cmd"
    assert not launcher.is_symlink()
    assert str(target.resolve()) in launcher.read_text(encoding="utf-8")


def test_windows_launcher_refuses_to_replace_user_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "venv" / "Scripts" / "trade.exe"
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    local_app_data = tmp_path / "local-app-data"
    launcher = local_app_data / "TradingAgent" / "bin" / "trade.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\nuser command\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    with pytest.raises(FileExistsError, match="refusing"):
        install_user_launcher(target, system="windows")


def test_launcher_target_uses_windows_console_script_name(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "Scripts" / "python.exe"

    assert launcher_target_for_interpreter(interpreter, system="windows") == (
        interpreter.parent / "trade.exe"
    )
    assert launcher_target_for_interpreter(interpreter, system="linux") == (
        interpreter.parent / "trade"
    )


@pytest.mark.parametrize(
    ("reported_system", "dependency", "expected"),
    [
        ("Darwin", "ollama", "brew install ollama"),
        ("Linux", "postgresql", "distribution package manager"),
        ("Windows", "ollama", "download/windows"),
    ],
)
def test_dependency_guidance_matches_platform(
    reported_system: str,
    dependency: str,
    expected: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.setup.platform.system", lambda: reported_system)

    assert expected in dependency_guidance(dependency)  # type: ignore[arg-type]


def test_linux_postgres_does_not_assume_homebrew(monkeypatch) -> None:
    monkeypatch.setattr("app.setup.operating_system", lambda: "linux")
    monkeypatch.setattr(
        "app.setup.start_homebrew_service",
        lambda _name: pytest.fail("Homebrew must not be used on Linux"),
    )

    started, detail = start_local_service("postgresql")

    assert started is False
    assert "docker compose" in detail
    assert "distribution package manager" in detail


def test_linux_ollama_starts_installed_binary_without_homebrew(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr("app.setup.operating_system", lambda: "linux")
    monkeypatch.setattr("app.setup.shutil.which", lambda _name: "/usr/bin/ollama")
    monkeypatch.setattr(
        "app.setup.start_homebrew_service",
        lambda _name: pytest.fail("Homebrew must not be used on Linux"),
    )
    monkeypatch.setattr(
        "app.setup.subprocess.Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    started, detail = start_local_service("ollama")

    assert started is True
    assert "background" in detail
    assert calls[0][0] == ["/usr/bin/ollama", "serve"]
    assert calls[0][1]["start_new_session"] is True

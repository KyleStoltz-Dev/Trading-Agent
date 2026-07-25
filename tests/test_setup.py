from pathlib import Path

import pytest

from app.setup import install_user_launcher, provider_settings, update_env_file


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

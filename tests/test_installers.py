import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_descriptive_installer_entrypoints_exist() -> None:
    for name in (
        "install-trading-agent.command",
        "install-trading-agent.sh",
        "install-trading-agent.ps1",
    ):
        assert (PROJECT_ROOT / name).is_file()


def test_primary_installers_include_hosted_provider_adapters() -> None:
    shell = _text("install-trading-agent.sh")
    powershell = _text("install-trading-agent.ps1")
    for content in (shell, powershell):
        assert "--require-hashes" in content
        assert "--only-binary=:all:" in content
        assert "requirements-bootstrap.txt" in content
        assert "sync --locked --inexact --extra ai" in content
        assert "pip install -e" not in content
    assert "--extra metatrader" not in shell
    assert "--extra metatrader" in powershell
    assert "--no-setup" in shell
    assert "$NoSetup" in powershell
    assert powershell.count("if ($LASTEXITCODE -ne 0)") >= 8


def test_uv_bootstrap_is_version_and_hash_pinned() -> None:
    bootstrap = _text("requirements-bootstrap.txt")
    hashes = {
        line.split("sha256:", 1)[1].split()[0].rstrip("\\")
        for line in bootstrap.splitlines()
        if "sha256:" in line
    }
    assert "uv==0.11.32" in bootstrap
    assert len(hashes) == 18
    assert all(len(digest) == 64 for digest in hashes)
    assert all(set(digest) <= set("0123456789abcdef") for digest in hashes)


def test_clean_install_exposes_both_hosted_provider_sdks() -> None:
    assert importlib.util.find_spec("openai") is not None
    assert importlib.util.find_spec("anthropic") is not None


def test_legacy_installers_are_thin_compatibility_wrappers() -> None:
    pairs = (
        ("install.command", "install-trading-agent.command"),
        ("install.sh", "install-trading-agent.sh"),
        ("install.ps1", "install-trading-agent.ps1"),
    )
    for legacy, primary in pairs:
        content = _text(legacy)
        assert primary in content
        assert "pip install" not in content
        assert "trade setup" not in content


def test_documentation_uses_descriptive_installer_names() -> None:
    for name in ("README.md", "docs/operations.md"):
        content = _text(name)
        assert "./install-trading-agent.command" in content
        assert "./install-trading-agent.sh" in content
        assert r".\install-trading-agent.ps1" in content

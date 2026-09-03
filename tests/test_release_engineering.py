import json
import os
import re
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import psycopg
import pytest

from scripts.generate_sbom import build_sbom
from scripts.run_migration_drill import (
    REVERSIBLE_FLOOR,
    MigrationDrillError,
    _head_and_floor,
    run_drill,
)
from scripts.verify_postgres_backup_restore import (
    BackupRestoreError,
    _postgres_environment,
    _validated_url,
    verify_backup_restore,
)
from scripts.verify_release_artifacts import (
    ArtifactVerificationError,
    _safe_member_name,
    check_release_metadata,
    verify_sdist,
    verify_wheel,
    write_release_metadata,
)


def _bootstrap_uv_version() -> str:
    requirements = (Path(__file__).resolve().parents[1] / "requirements-bootstrap.txt").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^uv==([0-9]+\.[0-9]+\.[0-9]+)", requirements, flags=re.MULTILINE)
    assert match is not None, "could not parse uv pin from requirements-bootstrap.txt"
    return match.group(1)


def test_release_member_validation_rejects_private_and_unsafe_paths() -> None:
    for name in (
        "../secret",
        "/absolute/path",
        "project/.data/private-journal.json",
        "project/.env.save",
        "project/app/cache.pyc",
        "project/.git/config",
    ):
        with pytest.raises(ArtifactVerificationError):
            _safe_member_name(name)

    assert _safe_member_name("project/.env.example").as_posix() == (
        "project/.env.example"
    )


def test_wheel_and_sdist_require_runtime_release_content(tmp_path) -> None:
    wheel = tmp_path / "trading_agent-0.1.0-py3-none-any.whl"
    metadata = b"Metadata-Version: 2.4\nName: trading-agent\nVersion: 0.1.0\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("app/trading-rules.json", "{}")
        archive.writestr("app/harness/HARNESS.md", "harness")
        archive.writestr("app/migrations/env.py", "")
        archive.writestr("app/migrations/versions/revision.py", "")
        archive.writestr("trading_agent-0.1.0.dist-info/METADATA", metadata)
        archive.writestr(
            "trading_agent-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "trade = app.cli:run\n"
            "trading-agent = app.cli:run\n"
            "trading-agent-mt5-bridge = app.metatrader_bridge_server:run\n",
        )
    assert verify_wheel(wheel, project_name="trading-agent", version="0.1.0").kind == (
        "wheel"
    )

    sdist = tmp_path / "trading_agent-0.1.0.tar.gz"
    required = {
        ".devcontainer/Dockerfile.secure": b"FROM example.invalid/base",
        ".devcontainer/codex-install/package-lock.json": b"{}",
        ".devcontainer/codex-install/package.json": b"{}",
        ".devcontainer/configure-codex-proxy.py": b"",
        ".devcontainer/container-entrypoint.sh": b"",
        ".devcontainer/devcontainer.secure.json": b"{}",
        ".devcontainer/init-firewall.sh": b"",
        ".devcontainer/post-create.sh": b"",
        ".devcontainer/responses-api-proxy.py": b"",
        ".dockerignore": b".env\n.data\n",
        "README.md": b"readme",
        "alembic.ini": b"",
        "app/harness/HARNESS.md": b"",
        "app/migrations/env.py": b"",
        "app/migrations/versions/revision.py": b"",
        "app/trading-rules.json": b"{}",
        "install-trading-agent.ps1": b"",
        "install-trading-agent.sh": b"",
        "pyproject.toml": b"",
        "requirements-bootstrap.txt": f"uv=={_bootstrap_uv_version()} --hash=sha256:".encode()
        + b"a" * 64,
        "uv.lock": b"",
    }
    with tarfile.open(sdist, "w:gz") as archive:
        for name, content in required.items():
            info = tarfile.TarInfo(f"trading_agent-0.1.0/{name}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
    assert verify_sdist(sdist, version="0.1.0").kind == "sdist"


def test_release_metadata_detects_artifact_tampering(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    dist = project / "dist"
    project.mkdir()
    dist.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "trading-agent"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (dist / "sbom.spdx.json").write_text(
        '{"spdxVersion":"SPDX-2.3","dataLicense":"CC0-1.0","packages":[]}\n',
        encoding="utf-8",
    )
    wheel = dist / "one.whl"
    sdist = dist / "one.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    from scripts.verify_release_artifacts import VerifiedArtifact

    artifacts = [
        VerifiedArtifact(wheel, "wheel", __import__("hashlib").sha256(b"wheel").hexdigest(), 5),
        VerifiedArtifact(sdist, "sdist", __import__("hashlib").sha256(b"sdist").hexdigest(), 5),
    ]
    monkeypatch.setattr(
        "scripts.verify_release_artifacts._git_commit",
        lambda _root: "a" * 40,
    )
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1785029682")
    write_release_metadata(dist, artifacts, project_root=project)
    check_release_metadata(dist, project_root=project)
    wheel.write_bytes(b"changed")
    with pytest.raises(ArtifactVerificationError, match="digest mismatch"):
        check_release_metadata(dist, project_root=project)


def test_sbom_is_deterministic_for_a_fixed_timestamp(tmp_path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "alpha"\nversion = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    first = build_sbom(lock, created="2026-07-27T12:00:00Z")
    second = build_sbom(lock, created="2026-07-27T12:00:00Z")
    assert first == second
    assert first["spdxVersion"] == "SPDX-2.3"
    assert first["packages"][0]["externalRefs"][0]["referenceLocator"] == (
        "pkg:pypi/alpha@1.2.3"
    )
    json.dumps(first)


def test_backup_configuration_uses_environment_without_leaking_database_url(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "do-not-inherit")
    monkeypatch.setenv("PGPASSWORD", "do-not-inherit")
    url = _validated_url(
        "postgresql+psycopg://trading:secret@localhost:5432/trading_agent"
        "?sslmode=require"
    )
    environment = _postgres_environment(url, database="restore_check")
    assert "DATABASE_URL" not in environment
    assert environment["PGDATABASE"] == "restore_check"
    assert environment["PGPASSWORD"] == "secret"
    assert environment["PGSSLMODE"] == "require"


def test_backup_rejects_non_postgresql_urls() -> None:
    with pytest.raises(BackupRestoreError, match="PostgreSQL"):
        _validated_url("sqlite:///local.sqlite3")


def test_backup_wraps_database_errors_without_echoing_connection_details(
    monkeypatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise psycopg.OperationalError("password=must-not-be-echoed")

    monkeypatch.setattr(
        "scripts.verify_postgres_backup_restore._verify_backup_restore",
        fail,
    )
    with pytest.raises(BackupRestoreError, match="OperationalError") as error:
        verify_backup_restore("postgresql://example.invalid/database")
    assert "must-not-be-echoed" not in str(error.value)


def test_migration_drill_refuses_unconfirmed_or_nonrelease_database() -> None:
    with pytest.raises(MigrationDrillError, match="exactly match"):
        run_drill(
            "postgresql+psycopg://user:pass@localhost/trading_agent_release_ci",
            confirmed_database="wrong",
        )
    with pytest.raises(MigrationDrillError, match="must start"):
        run_drill(
            "postgresql+psycopg://user:pass@localhost/production",
            confirmed_database="production",
        )


def test_migration_drill_floor_is_an_ancestor_of_the_single_head() -> None:
    head, floor = _head_and_floor()
    assert len(head) == 12
    assert floor == REVERSIBLE_FLOOR


def test_release_files_are_explicitly_packaged() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert "[tool.hatch.build.targets.sdist]" in content
    assert '"/.devcontainer"' in content
    assert '"/requirements-bootstrap.txt"' in content
    assert '"/scripts"' in content
    assert '"/uv.lock"' in content
    assert '"/.data"' in content
    assert '"/.env"' in content
    assert os.path.basename(pyproject) == "pyproject.toml"


def test_docker_context_excludes_credentials_and_private_data() -> None:
    dockerignore = (
        Path(__file__).resolve().parents[1] / ".dockerignore"
    ).read_text(encoding="utf-8")
    entries = set(dockerignore.splitlines())
    assert {".env", ".env.*", ".data", ".git", "*.key", "*.pem"} <= entries
    assert "!.env.example" in entries


def test_ci_exercises_secure_container_runtime_and_fail_closed_paths() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "git clone --no-hardlinks" in workflow
    assert "test -S /run/trading-agent/codex-proxy.sock" in workflow
    assert "bash -c '! command -v sudo'" in workflow
    assert "trading-agent-post-create" in workflow
    assert "/workspace/.venv/bin/trade --help" in workflow
    assert "git -C /workspace rev-parse --is-inside-work-tree" in workflow
    assert "http://127.0.0.1:3128/v1/responses" in workflow
    assert "http://127.0.0.1:3128/v1/models" in workflow
    assert "trading-agent-configure-codex-proxy --stdin" in workflow
    assert "ci-second-fake-key" in workflow
    assert "OPENAI_API_KEY|CODEX_API_KEY" in workflow
    assert '"VmLck:"' in workflow
    assert '"Max" && $2 == "core"' in workflow
    assert 'test "$upstream_status" = "401"' in workflow
    assert "previous_response_id" in workflow
    assert '"type":"mcp"' in workflow
    assert '"type":"web_search"' in workflow
    assert '"model":"unapproved-model"' in workflow
    assert '--user trading-egress "$container"' in workflow
    assert "https://api.openai.com" in workflow
    assert "https://1.1.1.1" in workflow
    assert "getent ahostsv4 example.com" in workflow
    assert "--network none" in workflow
    assert "did not resolve to an IPv4 address" in workflow
    assert "docker sbom" in workflow
    assert "grep -Eq -- '--format([=[:space:]]|$)'" in workflow
    assert "docker sbom with SPDX-JSON output is unavailable" in workflow
    assert "npm audit --omit=dev --audit-level=high" in workflow
    assert "uv export --locked --all-extras --all-groups --no-emit-project" in workflow
    assert "pip-audit --strict --no-deps --disable-pip" in workflow

#!/usr/bin/env python3
"""Inspect release archives and emit content-addressed release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENTRY_POINTS = {
    "trade = app.cli:run",
    "trading-agent = app.cli:run",
    "trading-agent-mt5-bridge = app.metatrader_bridge_server:run",
}
FORBIDDEN_PARTS = frozenset(
    {
        ".data",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "dist",
    }
)
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".sqlite", ".sqlite3")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactVerificationError(RuntimeError):
    """A candidate artifact is incomplete, contaminated, or inconsistent."""


@dataclass(frozen=True)
class VerifiedArtifact:
    path: Path
    kind: str
    sha256: str
    size: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ArtifactVerificationError(f"unsafe archive member path: {name!r}")
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        raise ArtifactVerificationError(f"forbidden release content: {name!r}")
    basename = path.name.casefold()
    if basename == ".env" or (
        basename.startswith(".env.") and basename != ".env.example"
    ):
        raise ArtifactVerificationError(f"private environment file in artifact: {name!r}")
    if basename.endswith(FORBIDDEN_SUFFIXES):
        raise ArtifactVerificationError(f"generated/private file in artifact: {name!r}")
    return path


def _verify_names(names: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        path = _safe_member_name(name)
        normalized.add(path.as_posix())
    return normalized


def _metadata_value(metadata: bytes, key: str) -> str:
    value = BytesParser().parsebytes(metadata).get(key)
    if value is None:
        raise ArtifactVerificationError(f"wheel metadata is missing {key}")
    return value


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > 5 * 1024 * 1024:
        raise ArtifactVerificationError(f"metadata member is unexpectedly large: {name}")
    return archive.read(info)


def verify_wheel(path: Path, *, project_name: str, version: str) -> VerifiedArtifact:
    try:
        with zipfile.ZipFile(path) as archive:
            names = _verify_names(archive.namelist())
            metadata_names = sorted(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            entry_point_names = sorted(
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            )
            if len(metadata_names) != 1 or len(entry_point_names) != 1:
                raise ArtifactVerificationError(
                    "wheel must contain exactly one METADATA and entry_points.txt"
                )
            metadata = _read_zip_member(archive, metadata_names[0])
            normalized_name = _metadata_value(metadata, "Name").replace("_", "-").casefold()
            if normalized_name != project_name.replace("_", "-").casefold():
                raise ArtifactVerificationError("wheel project name does not match pyproject.toml")
            if _metadata_value(metadata, "Version") != version:
                raise ArtifactVerificationError("wheel version does not match pyproject.toml")
            entry_points = _read_zip_member(archive, entry_point_names[0]).decode("utf-8")
            missing_entry_points = sorted(
                entry for entry in REQUIRED_ENTRY_POINTS if entry not in entry_points
            )
            if missing_entry_points:
                raise ArtifactVerificationError(
                    f"wheel is missing entry points: {missing_entry_points}"
                )
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ArtifactVerificationError(f"invalid wheel archive: {path.name}") from exc

    required_exact = {
        "app/trading-rules.json",
        "app/harness/HARNESS.md",
        "app/migrations/env.py",
    }
    missing = sorted(required_exact - names)
    if missing:
        raise ArtifactVerificationError(f"wheel is missing runtime files: {missing}")
    if not any(
        name.startswith("app/migrations/versions/") and name.endswith(".py")
        for name in names
    ):
        raise ArtifactVerificationError("wheel contains no database migration revisions")
    return VerifiedArtifact(path, "wheel", _sha256(path), path.stat().st_size)


def _tar_regular_names(archive: tarfile.TarFile) -> set[str]:
    names: set[str] = set()
    for member in archive.getmembers():
        name = _safe_member_name(member.name).as_posix()
        if member.issym() or member.islnk():
            raise ArtifactVerificationError(f"source archive contains a link: {member.name!r}")
        if not (member.isdir() or member.isfile()):
            raise ArtifactVerificationError(
                f"source archive contains a special file: {member.name!r}"
            )
        names.add(name)
    return names


def verify_sdist(path: Path, *, version: str) -> VerifiedArtifact:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            names = _tar_regular_names(archive)
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactVerificationError(f"invalid source archive: {path.name}") from exc
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise ArtifactVerificationError("source archive must have exactly one root directory")
    root = next(iter(roots))
    if not root.endswith(f"-{version}"):
        raise ArtifactVerificationError("source archive root does not match project version")
    required = {
        f"{root}/.devcontainer/Dockerfile.secure",
        f"{root}/.devcontainer/codex-install/package-lock.json",
        f"{root}/.devcontainer/codex-install/package.json",
        f"{root}/.devcontainer/configure-codex-proxy.py",
        f"{root}/.devcontainer/container-entrypoint.sh",
        f"{root}/.devcontainer/devcontainer.secure.json",
        f"{root}/.devcontainer/init-firewall.sh",
        f"{root}/.devcontainer/post-create.sh",
        f"{root}/.devcontainer/responses-api-proxy.py",
        f"{root}/.dockerignore",
        f"{root}/README.md",
        f"{root}/alembic.ini",
        f"{root}/app/harness/HARNESS.md",
        f"{root}/app/migrations/env.py",
        f"{root}/app/trading-rules.json",
        f"{root}/install-trading-agent.ps1",
        f"{root}/install-trading-agent.sh",
        f"{root}/pyproject.toml",
        f"{root}/requirements-bootstrap.txt",
        f"{root}/uv.lock",
    }
    missing = sorted(required - names)
    if missing:
        raise ArtifactVerificationError(f"source archive is missing release files: {missing}")
    if not any(
        name.startswith(f"{root}/app/migrations/versions/") and name.endswith(".py")
        for name in names
    ):
        raise ArtifactVerificationError("source archive contains no migration revisions")
    return VerifiedArtifact(path, "sdist", _sha256(path), path.stat().st_size)


def _project_metadata(project_root: Path) -> tuple[str, str]:
    with (project_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["name"]), str(project["version"])


def _git_commit(project_root: Path) -> str | None:
    supplied = os.environ.get("GITHUB_SHA")
    if supplied and re.fullmatch(r"[0-9a-fA-F]{40}", supplied):
        return supplied.casefold()
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed read-only args.
        [git, "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip().casefold()
    return commit if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def verify_dist(dist: Path, *, project_root: Path = PROJECT_ROOT) -> list[VerifiedArtifact]:
    if dist.is_symlink() or not dist.is_dir():
        raise ArtifactVerificationError("dist must be a real directory")
    project_name, version = _project_metadata(project_root)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ArtifactVerificationError(
            "dist must contain exactly one wheel and one .tar.gz source archive"
        )
    return [
        verify_wheel(wheels[0], project_name=project_name, version=version),
        verify_sdist(sdists[0], version=version),
    ]


def _write_text_safely(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ArtifactVerificationError(f"refusing to replace symlink: {path}")
    path.write_text(content, encoding="utf-8")


def write_release_metadata(
    dist: Path,
    artifacts: list[VerifiedArtifact],
    *,
    project_root: Path = PROJECT_ROOT,
) -> None:
    project_name, version = _project_metadata(project_root)
    commit = _git_commit(project_root)
    if commit is None:
        raise ArtifactVerificationError(
            "release provenance requires a Git commit or valid GITHUB_SHA"
        )
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not source_date_epoch or not source_date_epoch.isdigit():
        raise ArtifactVerificationError(
            "release provenance requires a numeric SOURCE_DATE_EPOCH"
        )
    checksums = "".join(
        f"{artifact.sha256}  {artifact.path.name}\n"
        for artifact in sorted(artifacts, key=lambda item: item.path.name)
    )
    _write_text_safely(dist / "SHA256SUMS", checksums)
    sbom = dist / "sbom.spdx.json"
    manifest = {
        "schema_version": 1,
        "project": project_name,
        "version": version,
        "git_commit": commit,
        "source_date_epoch": source_date_epoch,
        "lock_sha256": _sha256(project_root / "uv.lock"),
        "sbom_sha256": _sha256(sbom) if sbom.is_file() else None,
        "artifacts": [
            {
                "filename": artifact.path.name,
                "kind": artifact.kind,
                "sha256": artifact.sha256,
                "size": artifact.size,
            }
            for artifact in sorted(artifacts, key=lambda item: item.path.name)
        ],
    }
    _write_text_safely(
        dist / "release-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def check_release_metadata(
    dist: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> None:
    manifest_path = dist / "release-manifest.json"
    checksums_path = dist / "SHA256SUMS"
    if manifest_path.is_symlink() or checksums_path.is_symlink():
        raise ArtifactVerificationError("release metadata cannot be symbolic links")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_name, version = _project_metadata(project_root)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("project") != project_name
        or manifest.get("version") != version
    ):
        raise ArtifactVerificationError("release manifest project metadata is inconsistent")
    if not isinstance(manifest.get("git_commit"), str) or not re.fullmatch(
        r"[0-9a-f]{40}",
        manifest["git_commit"],
    ):
        raise ArtifactVerificationError("release manifest Git commit is invalid")
    expected_commit = _git_commit(project_root)
    if expected_commit is None or manifest["git_commit"] != expected_commit:
        raise ArtifactVerificationError("release manifest Git commit is inconsistent")
    source_date_epoch = manifest.get("source_date_epoch")
    if not isinstance(source_date_epoch, str) or not source_date_epoch.isdigit():
        raise ArtifactVerificationError("release manifest build timestamp is invalid")
    if manifest.get("lock_sha256") != _sha256(project_root / "uv.lock"):
        raise ArtifactVerificationError("release manifest lock digest is inconsistent")
    sbom_digest = manifest.get("sbom_sha256")
    sbom_path = dist / "sbom.spdx.json"
    if not isinstance(sbom_digest, str) or not SHA256_PATTERN.fullmatch(sbom_digest):
        raise ArtifactVerificationError("release manifest is missing the SBOM digest")
    if not sbom_path.is_file() or sbom_path.is_symlink() or _sha256(sbom_path) != sbom_digest:
        raise ArtifactVerificationError("release manifest SBOM digest mismatch")
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    if (
        sbom.get("spdxVersion") != "SPDX-2.3"
        or sbom.get("dataLicense") != "CC0-1.0"
        or not isinstance(sbom.get("packages"), list)
    ):
        raise ArtifactVerificationError("release SBOM is not a valid SPDX 2.3 package document")
    manifest_artifacts = manifest.get("artifacts")
    if not isinstance(manifest_artifacts, list) or len(manifest_artifacts) != 2:
        raise ArtifactVerificationError("release manifest must contain two package artifacts")
    for item in manifest_artifacts:
        if not isinstance(item, dict):
            raise ArtifactVerificationError("manifest artifact entries must be objects")
        filename = item.get("filename")
        digest = item.get("sha256")
        kind = item.get("kind")
        size = item.get("size")
        if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
            raise ArtifactVerificationError("manifest contains an invalid artifact filename")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise ArtifactVerificationError("manifest contains an invalid SHA-256 digest")
        path = dist / filename
        if not path.is_file() or path.is_symlink() or _sha256(path) != digest:
            raise ArtifactVerificationError(f"manifest digest mismatch: {filename}")
        if not isinstance(size, int) or size != path.stat().st_size:
            raise ArtifactVerificationError(f"manifest size mismatch: {filename}")
        expected_kind = "wheel" if filename.endswith(".whl") else "sdist"
        if kind != expected_kind:
            raise ArtifactVerificationError(f"manifest kind mismatch: {filename}")
    if {item["kind"] for item in manifest_artifacts} != {"wheel", "sdist"}:
        raise ArtifactVerificationError("release manifest artifact kinds are incomplete")
    expected: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if (
            len(parts) != 2
            or not SHA256_PATTERN.fullmatch(parts[0])
            or PurePosixPath(parts[1]).name != parts[1]
            or parts[1] in expected
        ):
            raise ArtifactVerificationError("SHA256SUMS contains an invalid entry")
        expected[parts[1]] = parts[0]
    if len(expected) != len(manifest_artifacts):
        raise ArtifactVerificationError("SHA256SUMS contains unexpected artifacts")
    for item in manifest_artifacts:
        if expected.get(item["filename"]) != item["sha256"]:
            raise ArtifactVerificationError(
                f"SHA256SUMS mismatch: {item['filename']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument(
        "--write-metadata",
        action="store_true",
        help="write SHA256SUMS and release-manifest.json after verification",
    )
    parser.add_argument(
        "--check-metadata",
        action="store_true",
        help="verify existing checksums and release-manifest.json",
    )
    args = parser.parse_args()
    dist = args.dist.expanduser().resolve()
    try:
        artifacts = verify_dist(dist)
        if args.write_metadata:
            write_release_metadata(dist, artifacts)
        if args.check_metadata:
            check_release_metadata(dist)
    except (ArtifactVerificationError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        "Verified release artifacts: "
        + ", ".join(f"{item.path.name} ({item.sha256[:12]})" for item in artifacts)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

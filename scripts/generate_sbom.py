#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 SBOM from the checked-in uv lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _spdx_id(name: str, version: str, index: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{name}-{version}").strip("-")
    return f"SPDXRef-Package-{safe or index}-{index}"


def build_sbom(lock_path: Path, *, created: str | None = None) -> dict:
    with lock_path.open("rb") as handle:
        lock = tomllib.load(handle)
    lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    timestamp = created or datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    packages = []
    relationships = []
    for index, package in enumerate(
        sorted(
            lock.get("package", []),
            key=lambda value: (str(value.get("name", "")), str(value.get("version", ""))),
        ),
        start=1,
    ):
        name = str(package["name"])
        version = str(package.get("version", "0+unknown"))
        identifier = _spdx_id(name, version, index)
        source = package.get("source", {})
        registry = source.get("registry") if isinstance(source, dict) else None
        download = (
            f"{str(registry).rstrip('/')}/{quote(name)}"
            if registry
            else "NOASSERTION"
        )
        packages.append(
            {
                "SPDXID": identifier,
                "name": name,
                "versionInfo": version,
                "downloadLocation": download,
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{quote(name)}@{quote(version)}",
                    }
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": identifier,
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "trading-agent-locked-environment",
        "documentNamespace": f"https://github.com/KyleStoltz-Dev/Trading-Agent/sbom/{lock_digest}",
        "creationInfo": {
            "created": timestamp,
            "creators": ["Tool: trading-agent/scripts/generate_sbom.py"],
        },
        "documentDescribes": [item["SPDXID"] for item in packages],
        "packages": packages,
        "relationships": relationships,
        "annotations": [
            {
                "annotationType": "OTHER",
                "annotator": "Tool: trading-agent/scripts/generate_sbom.py",
                "annotationDate": timestamp,
                "comment": f"Generated from uv.lock sha256:{lock_digest}",
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=PROJECT_ROOT / "uv.lock")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist" / "sbom.spdx.json",
    )
    parser.add_argument(
        "--created",
        help="fixed RFC3339 timestamp for reproducible output",
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.is_symlink():
        parser.error("SBOM output cannot be a symbolic link")
    output.parent.mkdir(parents=True, exist_ok=True)
    sbom = build_sbom(args.lock.expanduser().resolve(), created=args.created)
    output.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote SPDX SBOM: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

HARNESS_ROOT = Path(__file__).with_name("harness")
MAX_RESOURCE_BYTES = 32_000


@dataclass(frozen=True)
class HarnessResource:
    path: str
    description: str
    content: str
    sha256: str
    score: int


@dataclass(frozen=True)
class HarnessContext:
    resources: tuple[HarnessResource, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(resource.path for resource in self.resources)

    def render(self) -> str:
        sections = [
            (
                f"[Harness resource: {resource.path}; "
                f"sha256={resource.sha256[:12]}]\n{resource.content}"
            )
            for resource in self.resources
        ]
        return "\n\n".join(sections)


def _parse_document(path: Path) -> tuple[dict[str, str], str]:
    data = path.read_bytes()
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError(f"harness resource is too large: {path.name}")
    text = data.decode("utf-8")
    if not text.startswith("---\n"):
        return {}, text.strip()
    marker = text.find("\n---\n", 4)
    if marker == -1:
        return {}, text.strip()
    metadata: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[marker + 5 :].strip()


def _terms(value: str) -> tuple[str, ...]:
    return tuple(term.strip().lower() for term in value.split(",") if term.strip())


def _score(message: str, metadata: dict[str, str]) -> int:
    normalized = re.sub(r"\s+", " ", message.lower())
    score = 0
    for trigger in _terms(metadata.get("triggers", "")):
        if trigger in normalized:
            score += 3 if " " in trigger else 1
    return score


def _resource(path: Path, root: Path, score: int) -> HarnessResource:
    metadata, body = _parse_document(path)
    relative = path.relative_to(root).as_posix()
    return HarnessResource(
        path=relative,
        description=metadata.get("description", ""),
        content=body,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        score=score,
    )


def select_harness_context(
    message: str,
    *,
    root: Path = HARNESS_ROOT,
    max_resources: int = 5,
    max_characters: int = 12_000,
    excluded_prefixes: tuple[str, ...] = (),
    required_paths: tuple[str, ...] = (),
) -> HarnessContext:
    resolved_root = root.resolve()
    entrypoint = resolved_root / "HARNESS.md"
    if not entrypoint.is_file():
        return HarnessContext(())

    selected = [_resource(entrypoint, resolved_root, score=10_000)]
    selected_paths = {selected[0].path}
    used = len(selected[0].content)
    for relative in dict.fromkeys(required_paths):
        candidate_path = resolved_root / relative
        resolved = candidate_path.resolve()
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or candidate_path.is_symlink()
            or not resolved.is_relative_to(resolved_root)
            or not candidate_path.is_file()
            or candidate_path.suffix != ".md"
            or any(relative.startswith(prefix) for prefix in excluded_prefixes)
        ):
            raise ValueError(f"invalid required harness resource: {relative}")
        candidate = _resource(candidate_path, resolved_root, score=9_000)
        if candidate.path in selected_paths:
            continue
        if len(selected) >= max_resources:
            break
        if used + len(candidate.content) > max_characters:
            raise ValueError(f"required harness resources exceed context limit: {relative}")
        selected.append(candidate)
        selected_paths.add(candidate.path)
        used += len(candidate.content)

    candidates: list[HarnessResource] = []
    for path in sorted(resolved_root.rglob("*.md")):
        resolved = path.resolve()
        if path == entrypoint or path.is_symlink() or not resolved.is_relative_to(resolved_root):
            continue
        relative = path.relative_to(resolved_root).as_posix()
        if any(relative.startswith(prefix) for prefix in excluded_prefixes):
            continue
        metadata, _ = _parse_document(path)
        score = _score(message, metadata)
        if score > 0:
            candidates.append(_resource(path, resolved_root, score))

    candidates.sort(key=lambda item: (-item.score, item.path))
    for candidate in candidates:
        if len(selected) >= max_resources:
            break
        if candidate.path in selected_paths:
            continue
        if used + len(candidate.content) > max_characters:
            continue
        selected.append(candidate)
        selected_paths.add(candidate.path)
        used += len(candidate.content)
    return HarnessContext(tuple(selected))

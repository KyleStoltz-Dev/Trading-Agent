"""Pure terminal text formatting, independent of storage and model execution."""

import re
import unicodedata

_DOCUMENT_FENCE = re.compile(
    r"```(?P<language>[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_TABLE_DIVIDER_CELL = re.compile(r"^:?-{3,}:?$")


def _looks_like_markdown_document(value: str) -> bool:
    lines = value.splitlines()
    headings = sum(bool(re.match(r"^\s{0,3}#{1,6}\s+", line)) for line in lines)
    tables = any(
        index + 1 < len(lines)
        and "|" in line
        and all(
            _TABLE_DIVIDER_CELL.fullmatch(cell.strip())
            for cell in lines[index + 1].strip().strip("|").split("|")
        )
        for index, line in enumerate(lines)
        if line.strip().startswith("|")
    )
    return headings > 0 and (tables or "**" in value or headings > 1)


def _unwrap_document_fences(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        language = match.group("language").casefold()
        body = match.group("body").strip()
        if language in {"markdown", "md"}:
            return body
        if not language and _looks_like_markdown_document(body):
            return body
        return match.group(0)

    return _DOCUMENT_FENCE.sub(replace, value)


def _markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


_TABLE_HEADER_LABELS = {
    "hypothetical description": "Working idea",
    "encoding": "What must be defined",
    "current state this session window": "Status",
    "action required": "Next step",
    "action required from you before proceeding": "Next step",
}


def _terminal_table_header(value: str) -> str:
    return _TABLE_HEADER_LABELS.get(value.strip().casefold(), value.strip())


def _stack_markdown_tables(value: str) -> str:
    lines = value.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or "|" not in lines[index]:
            rendered.append(lines[index])
            index += 1
            continue
        headers = _markdown_cells(lines[index])
        divider = _markdown_cells(lines[index + 1])
        if (
            len(headers) < 2
            or len(headers) != len(divider)
            or not all(_TABLE_DIVIDER_CELL.fullmatch(cell) for cell in divider)
        ):
            rendered.append(lines[index])
            index += 1
            continue
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and "|" in lines[index]:
            row = _markdown_cells(lines[index])
            if len(row) != len(headers):
                break
            rows.append(row)
            index += 1
        for row in rows:
            if rendered and rendered[-1]:
                rendered.append("")
            title = row[0].strip()
            if title and len(title) <= 72 and "\n" not in title:
                rendered.append(f"### {title}")
            else:
                rendered.extend((f"**{headers[0]}**", title))
            for header, cell in zip(headers[1:], row[1:], strict=True):
                label = _terminal_table_header(header)
                if label == "What must be defined":
                    cell = re.sub(r"(?i)^need:\s*", "", cell)
                rendered.extend(("", f"**{label}**", cell))
        if not rows:
            rendered.extend(
                [
                    " · ".join(headers),
                    " · ".join(divider),
                ]
            )
    return "\n".join(rendered)


_TERMINAL_CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
_TERMINAL_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_INTERNAL_IDENTIFIER = re.compile(r"(?<![/.])\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b(?:\.\.\.)?")
_BRACKETED_INTERNAL_IDENTIFIER = re.compile(r"\[(?P<name>[a-z][a-z0-9]*(?:_[a-z0-9]+)+)\]")
_INTERNAL_LABELS = {
    "edge_requires_evidence": "evidence requirement",
    "validate_strategy_draft": "strategy review",
    "create_strategy_version": "strategy save",
}


def _humanize_terminal_prose(value: str) -> str:
    """Keep model implementation jargon out of the normal trader-facing display."""

    def friendly_identifier(value: str) -> str:
        name = value.removesuffix("...")
        if name.startswith("create_trade_plan_function_call"):
            return "confirmed journal save"
        return _INTERNAL_LABELS.get(name, name.replace("_", " "))

    def humanize_identifier(match: re.Match[str]) -> str:
        return friendly_identifier(match.group(0))

    def humanize_bracketed(match: re.Match[str]) -> str:
        return friendly_identifier(match.group("name"))

    parts = _TERMINAL_CODE.split(value)
    for index in range(0, len(parts), 2):
        prose = parts[index]
        prose = _BRACKETED_INTERNAL_IDENTIFIER.sub(humanize_bracketed, prose)
        prose = _INTERNAL_IDENTIFIER.sub(humanize_identifier, prose)
        prose = re.sub(
            r"(?i)(?:unavailable\s*)?❌\s*(?:disabled|unavailable)?\s*",
            "Unavailable — ",
            prose,
        )
        prose = re.sub(
            r"(?i)(?:available\s*)?✅\s*(?:available)?\s*",
            "Available — ",
            prose,
        )
        prose = prose.replace("⚠️", "Caution —").replace("⚠", "Caution —")
        parts[index] = prose
    return "".join(parts)


def _space_dense_terminal_questions(value: str) -> str:
    """Make model-generated clarification requests readable in a terminal."""

    parts = _TERMINAL_CODE.split(value)
    for index in range(0, len(parts), 2):
        blocks = re.split(r"(\n[ \t]*\n)", parts[index])
        for block_index in range(0, len(blocks), 2):
            block = blocks[block_index]
            stripped = block.strip()
            if stripped.count("?") < 2 or any(
                line.lstrip().startswith(("#", "-", "*", ">", "|"))
                for line in stripped.splitlines()
            ):
                continue
            sentences = _TERMINAL_SENTENCE_BOUNDARY.split(re.sub(r"[ \t]*\n[ \t]*", " ", stripped))
            groups: list[list[str]] = []
            current: list[str] = []
            for sentence in sentences:
                if "?" in sentence and current:
                    groups.append(current)
                    current = []
                current.append(sentence)
            if current:
                groups.append(current)
            if len(groups) < 2:
                continue
            leading = block[: len(block) - len(block.lstrip())]
            trailing = block[len(block.rstrip()) :]
            blocks[block_index] = (
                leading + "\n\n".join(" ".join(group) for group in groups) + trailing
            )
        parts[index] = "".join(blocks)
    return "".join(parts)


def _terminal_markdown(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    safe = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    safe = _unwrap_document_fences(safe)
    safe = _stack_markdown_tables(safe)
    safe = _humanize_terminal_prose(safe)
    safe = _space_dense_terminal_questions(safe)
    safe = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "### ", safe)
    safe = re.sub(r"(?m)^(?:[ \t]*[-*_][ \t]*){3,}$", "", safe)
    safe = re.sub(r"\n{3,}", "\n\n", safe)
    return safe.strip()

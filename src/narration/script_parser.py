"""Fail-closed parser for the official spoken script Markdown."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


FINAL_SECTION = "## Narração final"
UNMARKED_NARRATION_TEXT = "UNMARKED_NARRATION_TEXT"
BEAT_HEADING = re.compile(r"^###\s+(B\d{3})\s+[—-]\s+(.+?)\s*$")
ANY_HEADING = re.compile(r"^#{3,6}\s+\S")


class NarrationScriptError(ValueError):
    """The official script is ambiguous or structurally invalid."""


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text, flags=re.UNICODE))


def parse_narration_script(
    path: str | Path,
    *,
    source_file: str | None = None,
) -> list[dict[str, Any]]:
    """Compile explicitly quoted beats from ``## Narração final`` only."""

    script_path = Path(path)
    text = script_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    section_starts = [
        index for index, line in enumerate(lines) if line.strip() == FINAL_SECTION
    ]
    if len(section_starts) != 1:
        raise NarrationScriptError(
            "official script must contain exactly one '## Narração final' section"
        )
    start = section_starts[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )

    parsed: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    quote_lines: list[str] = []
    quote_parts: list[str] = []

    def flush_quote() -> None:
        if quote_lines:
            quote_parts.append("\n".join(quote_lines))
            quote_lines.clear()

    def finish_beat() -> None:
        nonlocal current, quote_parts
        if current is None:
            return
        flush_quote()
        raw_text = "\n\n".join(quote_parts)
        if not raw_text.strip():
            raise NarrationScriptError(
                f"{current['beat_id']}: beat must contain non-empty blockquote text"
            )
        current["raw_text"] = raw_text
        current["source_hash"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        current["word_count"] = _word_count(raw_text)
        parsed.append(current)
        current = None
        quote_parts = []

    for index in range(start, end):
        raw_line = lines[index]
        stripped = raw_line.strip()
        line_number = index + 1
        beat_match = BEAT_HEADING.fullmatch(stripped)
        if beat_match:
            finish_beat()
            beat_id, title = beat_match.groups()
            current = {
                "beat_id": beat_id,
                "title": title,
                "source_file": source_file or script_path.as_posix(),
            }
            continue
        if ANY_HEADING.match(stripped):
            if re.match(r"^###\s+B\d", stripped):
                raise NarrationScriptError(
                    f"invalid beat heading at line {line_number}: {stripped}"
                )
            finish_beat()
            continue
        if not stripped:
            flush_quote()
            continue
        if stripped.startswith("- "):
            flush_quote()
            continue
        if raw_line.lstrip().startswith(">"):
            if current is None:
                raise NarrationScriptError(
                    f"{UNMARKED_NARRATION_TEXT}: blockquote outside beat at line {line_number}"
                )
            quote_text = raw_line.lstrip()[1:]
            if quote_text.startswith(" "):
                quote_text = quote_text[1:]
            quote_lines.append(quote_text)
            continue
        raise NarrationScriptError(
            f"{UNMARKED_NARRATION_TEXT}: line {line_number}: {stripped}"
        )
    finish_beat()

    if not parsed:
        raise NarrationScriptError("official narration must contain at least one beat")
    ids = [beat["beat_id"] for beat in parsed]
    duplicates = sorted({beat_id for beat_id in ids if ids.count(beat_id) > 1})
    if duplicates:
        raise NarrationScriptError("duplicate beat IDs: " + ", ".join(duplicates))
    if ids[0] != "B001":
        raise NarrationScriptError("official narration must start with B001")
    expected = [f"B{index:03d}" for index in range(1, len(ids) + 1)]
    if ids != expected:
        mismatch_index = next(
            index
            for index, (actual, wanted) in enumerate(zip(ids, expected))
            if actual != wanted
        )
        raise NarrationScriptError(
            "beat IDs must be sequential and preserve document order: "
            f"expected {expected[mismatch_index]}, got {ids[mismatch_index]}"
        )
    return parsed


def spoken_text(beats: list[dict[str, Any]]) -> str:
    """Return canonical speech order for migration verification."""

    return "\n\n".join(str(beat["raw_text"]) for beat in beats)

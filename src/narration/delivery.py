"""Explicit, episode-scoped delivery annotations for Azure narration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr


class DeliveryCueError(ValueError):
    pass


PROSODY_FIELDS = ("rate", "pitch", "volume", "contour")


def _occurrences(raw_text: str, needle: str) -> list[int]:
    """Return every occurrence, including overlapping occurrences."""

    positions: list[int] = []
    cursor = 0
    while True:
        position = raw_text.find(needle, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + 1


def _unique_range(beat_id: str, kind: str, raw_text: str, text: Any) -> tuple[int, int]:
    if not isinstance(text, str) or not text:
        raise DeliveryCueError(f"{beat_id}: {kind} text must be non-empty")
    matches = _occurrences(raw_text, text)
    if len(matches) != 1:
        raise DeliveryCueError(
            f"{beat_id}: {kind} text must occur exactly once; "
            f"found {len(matches)}: {text!r}"
        )
    return matches[0], matches[0] + len(text)


def _validated_pauses(
    beat_id: str, raw_text: str, raw_cues: Any
) -> list[dict[str, Any]]:
    if not isinstance(raw_cues, list):
        raise DeliveryCueError(f"{beat_id}: pause_after must be a list")
    cues: list[dict[str, Any]] = []
    positions: list[int] = []
    for index, cue in enumerate(raw_cues, 1):
        if not isinstance(cue, dict) or set(cue) != {"text", "milliseconds"}:
            raise DeliveryCueError(f"{beat_id}: pause_after #{index} has invalid fields")
        start, _end = _unique_range(beat_id, "pause_after", raw_text, cue["text"])
        milliseconds = cue["milliseconds"]
        if (
            isinstance(milliseconds, bool)
            or not isinstance(milliseconds, int)
            or not 1 <= milliseconds <= 10_000
        ):
            raise DeliveryCueError(
                f"{beat_id}: pause_after #{index} milliseconds must be an integer from 1 to 10000"
            )
        positions.append(start)
        cues.append({"text": cue["text"], "milliseconds": milliseconds})
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise DeliveryCueError(f"{beat_id}: pause_after cues must follow spoken-text order")
    return cues


def _validated_prosody(
    beat_id: str, raw_text: str, raw_ranges: Any
) -> list[dict[str, str]]:
    if not isinstance(raw_ranges, list):
        raise DeliveryCueError(f"{beat_id}: prosody must be a list")
    ranges: list[tuple[int, int, dict[str, str]]] = []
    allowed = {"text", *PROSODY_FIELDS}
    for index, item in enumerate(raw_ranges, 1):
        if not isinstance(item, dict) or "text" not in item or not set(item) <= allowed:
            raise DeliveryCueError(f"{beat_id}: prosody #{index} has invalid fields")
        controls = {field: item[field] for field in PROSODY_FIELDS if field in item}
        if not controls:
            raise DeliveryCueError(f"{beat_id}: prosody #{index} has no controls")
        if any(not isinstance(value, str) or not value.strip() for value in controls.values()):
            raise DeliveryCueError(f"{beat_id}: prosody #{index} controls must be non-empty strings")
        start, end = _unique_range(beat_id, "prosody", raw_text, item["text"])
        normalized = {"text": item["text"], **controls}
        ranges.append((start, end, normalized))
    ranges.sort(key=lambda entry: (entry[0], entry[1]))
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise DeliveryCueError(f"{beat_id}: prosody ranges must not overlap")
    return [item for _start, _end, item in ranges]


def _normalize_settings(beat_id: str, raw_text: str, settings: Any) -> dict[str, list[dict[str, Any]]]:
    # Compatibility with the first pause-only API, where a beat mapped directly
    # to its pause_after list.
    if isinstance(settings, list):
        settings = {"pause_after": settings}
    if not isinstance(settings, dict) or not set(settings) <= {"pause_after", "prosody"}:
        raise DeliveryCueError(
            f"{beat_id}: delivery settings may contain only pause_after and prosody"
        )
    return {
        "pause_after": _validated_pauses(
            beat_id, raw_text, settings.get("pause_after", [])
        ),
        "prosody": _validated_prosody(
            beat_id, raw_text, settings.get("prosody", [])
        ),
    }


def load_delivery_cues(
    path: Path, beats: list[dict[str, Any]]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != "1.0" or not isinstance(payload.get("beats"), dict):
        raise DeliveryCueError(
            "narration_delivery.json must use schema_version 1.0 and a beats object"
        )
    raw_by_id = {beat["beat_id"]: beat["raw_text"] for beat in beats}
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for beat_id, settings in payload["beats"].items():
        if beat_id not in raw_by_id:
            raise DeliveryCueError(f"{beat_id}: delivery cue references an unknown beat")
        result[beat_id] = _normalize_settings(beat_id, raw_by_id[beat_id], settings)
    return result


def synthesis_input_hash(
    beat: dict[str, Any], *, provider: str, voice: str, rate: str, pitch: str,
    output_format: str | None = None,
) -> str:
    canonical = {
        "raw_text": beat["raw_text"],
        "provider": provider,
        "voice": voice,
        "rate": rate,
        "pitch": pitch,
        "delivery_cues": beat.get("delivery_cues", []),
    }
    # Omitting an empty prosody list intentionally preserves every existing
    # pause-only official cache hash.
    if beat.get("prosody"):
        canonical["prosody"] = beat["prosody"]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    base_hash = hashlib.sha256(encoded).hexdigest()
    if output_format is None:
        return base_hash
    # Keep this envelope identical to the 48 kHz benchmark identity so a
    # benchmark WAV can be promoted only when every synthesis input matches.
    promoted = {
        "synthesis_input_hash": base_hash,
        "voice": voice,
        "rate": rate,
        "pitch": pitch,
        "output_format": output_format,
    }
    return hashlib.sha256(json.dumps(
        promoted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def apply_delivery_cues(
    beats: list[dict[str, Any]], cues_by_beat: dict[str, Any],
    *, provider: str, voice: str, rate: str, pitch: str,
    output_format: str | None = None,
) -> list[dict[str, Any]]:
    beat_ids = {beat["beat_id"] for beat in beats}
    unknown = sorted(set(cues_by_beat) - beat_ids)
    if unknown:
        raise DeliveryCueError(f"delivery cues reference unknown beats: {', '.join(unknown)}")
    prepared = []
    for original in beats:
        beat = dict(original)
        settings = _normalize_settings(
            beat["beat_id"], beat["raw_text"], cues_by_beat.get(beat["beat_id"], {})
        )
        beat["delivery_cues"] = settings["pause_after"]
        if settings["prosody"]:
            beat["prosody"] = settings["prosody"]
        else:
            beat.pop("prosody", None)
        beat["synthesis_input_hash"] = synthesis_input_hash(
            beat, provider=provider, voice=voice, rate=rate, pitch=pitch,
            output_format=output_format,
        )
        prepared.append(beat)
    return prepared


def ssml_body(
    beat: dict[str, Any], *, base_prosody: dict[str, str] | None = None
) -> str:
    """Compile raw text and all annotations deterministically in one pass."""

    beat_id = str(beat["beat_id"])
    raw_text = str(beat["raw_text"])
    settings = _normalize_settings(
        beat_id,
        raw_text,
        {
            "pause_after": beat.get("delivery_cues", []),
            "prosody": beat.get("prosody", []),
        },
    )
    ranges: list[tuple[int, int, dict[str, str]]] = []
    pauses: dict[int, list[int]] = {}
    for item in settings["prosody"]:
        start, end = _unique_range(beat_id, "prosody", raw_text, item["text"])
        ranges.append((start, end, item))
    for cue in settings["pause_after"]:
        _start, end = _unique_range(beat_id, "pause_after", raw_text, cue["text"])
        pauses.setdefault(end, []).append(int(cue["milliseconds"]))

    points = sorted({
        0,
        len(raw_text),
        *(start for start, _end, _item in ranges),
        *(end for _start, end, _item in ranges),
        *pauses,
    })
    parts: list[str] = []
    for index, point in enumerate(points):
        for milliseconds in pauses.get(point, []):
            parts.append(f'<break time="{milliseconds}ms"/>')
        if index == len(points) - 1:
            continue
        following = points[index + 1]
        controls = dict(base_prosody or {})
        active = next(
            (item for start, end, item in ranges if start <= point < end),
            None,
        )
        if active:
            controls.update({field: active[field] for field in PROSODY_FIELDS if field in active})
        content = escape(raw_text[point:following])
        if controls:
            attributes = " ".join(
                f"{field}={quoteattr(controls[field])}"
                for field in PROSODY_FIELDS
                if field in controls
            )
            parts.append(f"<prosody {attributes}>{content}</prosody>")
        else:
            parts.append(content)
    return "".join(parts)

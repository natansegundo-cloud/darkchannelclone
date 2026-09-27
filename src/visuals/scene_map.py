"""Build the editorial scene timeline from real official WordBoundary timing."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


class SceneMapError(RuntimeError):
    pass


def _normalized(value: str) -> str:
    value = "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )
    return "".join(re.findall(r"[a-z0-9]+", value))


def _anchor_tokens(value: str) -> list[str]:
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )
    return re.findall(r"[a-z0-9]+", folded)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SceneMapError("roteiro_visual.csv has no editorial scenes")
    ids = [str(row.get("scene_id") or "").strip() for row in rows]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise SceneMapError("roteiro_visual.csv has empty or duplicate scene_id")
    if any(not str(row.get("narration_anchor") or "").strip() for row in rows):
        raise SceneMapError("every editorial scene requires narration_anchor")
    return rows


def _official_words(timing: dict[str, Any]) -> list[dict[str, Any]]:
    if timing.get("timing_quality") != "WORD_BOUNDARY_REAL":
        raise SceneMapError("official timing must be WORD_BOUNDARY_REAL")
    if timing.get("same_synthesis_audio_timing") is not True:
        raise SceneMapError("official audio and timing must come from the same synthesis")
    words: list[dict[str, Any]] = []
    for beat in timing.get("beats", []):
        beat_id = str(beat.get("beat_id") or "")
        for word in beat.get("words", []):
            normalized = _normalized(str(word.get("normalized") or word.get("text") or ""))
            if not normalized:
                continue
            words.append({
                "text": str(word.get("text") or ""),
                "normalized": normalized,
                "start": float(word["start"]), "end": float(word["end"]),
                "beat_id": beat_id,
            })
    if not words:
        raise SceneMapError("official timing has no real words")
    return words


def _phrase_candidates(words: list[dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    needle = "".join(tokens)
    candidates: list[dict[str, Any]] = []
    for start in range(len(words)):
        value = ""
        for end in range(start, len(words)):
            value += words[end]["normalized"]
            if value == needle:
                candidates.append({"word_start": start, "word_end": end, "matched": " ".join(tokens)})
                break
            if len(value) >= len(needle):
                break
    return candidates


def _candidates(words: list[dict[str, Any]], anchor: str) -> list[dict[str, Any]]:
    tokens = _anchor_tokens(anchor)
    exact = _phrase_candidates(words, tokens)
    if exact:
        return [{**candidate, "resolution": "EXACT_WORD_BOUNDARY"} for candidate in exact]
    # Editorial anchors may paraphrase their sentence. Resolve only a longest
    # unique contiguous subphrase; anything ambiguous remains a hard failure.
    for size in range(len(tokens) - 1, 1, -1):
        found: list[dict[str, Any]] = []
        for offset in range(len(tokens) - size + 1):
            subset = tokens[offset:offset + size]
            found.extend(_phrase_candidates(words, subset))
        unique = {(item["word_start"], item["word_end"]): item for item in found}
        if len(unique) == 1:
            return [{**next(iter(unique.values())), "resolution": "SEMANTIC_UNIQUE_SUBPHRASE"}]
    return []


def _resolve_anchors(rows: list[dict[str, str]], words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    choices = [_candidates(words, row["narration_anchor"]) for row in rows]
    for row, candidates in zip(rows, choices):
        if not candidates:
            raise SceneMapError(f"{row['scene_id']}: narration anchor was not found")
    solutions: list[list[dict[str, Any]]] = []

    def visit(index: int, previous: int, selected: list[dict[str, Any]]) -> None:
        if len(solutions) > 1:
            return
        if index == len(choices):
            solutions.append(list(selected))
            return
        for candidate in choices[index]:
            if candidate["word_start"] > previous:
                selected.append(candidate)
                visit(index + 1, candidate["word_start"], selected)
                selected.pop()

    visit(0, -1, [])
    if len(solutions) != 1:
        raise SceneMapError("narration anchors do not have one unambiguous chronological mapping")
    return solutions[0]


def _character_presence(row: dict[str, str], visual: dict[str, Any] | None) -> str:
    if visual is not None:
        return str(visual.get("character_presence") or "NONE")
    scene_type = row["scene_type"]
    if scene_type == "CHARACTER_SCENE":
        return "FIN"
    if scene_type == "ENVIRONMENT_SCENE" and "fin" in row["visual"].casefold():
        return "FIN"
    return "NONE"


def build_scene_map(
    *, visual_script_path: Path, visual_scenes_path: Path, timing_path: Path,
    narration_path: Path, existing_scene_map_path: Path | None = None,
) -> dict[str, Any]:
    rows = _load_rows(visual_script_path)
    timing = json.loads(timing_path.read_text(encoding="utf-8-sig"))
    episode_id = str(timing.get("episode_id") or "")
    if not episode_id:
        raise SceneMapError("official timing has no episode_id")
    duration = float(timing.get("duration_seconds") or 0)
    if duration <= 0:
        raise SceneMapError("official duration must be positive")
    visual_payload = json.loads(visual_scenes_path.read_text(encoding="utf-8-sig"))
    if visual_payload.get("episode_id") != episode_id:
        raise SceneMapError("visual_scenes.json episode_id differs from official timing")
    visuals = {scene["scene_id"]: scene for scene in visual_payload.get("scenes", [])}
    if any(scene_id not in {row["scene_id"] for row in rows} for scene_id in visuals):
        raise SceneMapError("visual_scenes.json contains a scene absent from roteiro_visual.csv")
    previous: dict[str, dict[str, Any]] = {}
    if existing_scene_map_path and existing_scene_map_path.is_file():
        old = json.loads(existing_scene_map_path.read_text(encoding="utf-8-sig"))
        previous = {item["scene_id"]: item for item in old.get("scenes", [])}

    words = _official_words(timing)
    anchors = _resolve_anchors(rows, words)
    boundaries = [0.0] + [words[item["word_start"]]["start"] for item in anchors[1:]] + [duration]
    scenes: list[dict[str, Any]] = []
    for index, (row, anchor) in enumerate(zip(rows, anchors)):
        start, end = boundaries[index], boundaries[index + 1]
        if end <= start:
            raise SceneMapError(f"{row['scene_id']}: non-positive scene duration")
        interval_words = [word for word in words if word["start"] >= start and word["start"] < end]
        if not interval_words:
            raise SceneMapError(f"{row['scene_id']}: scene interval has no spoken words")
        beat_ids = list(dict.fromkeys(word["beat_id"] for word in interval_words))
        preserved = {
            key: value for key, value in previous.get(row["scene_id"], {}).items()
            if key in {"fin_role", "visual_intent", "setting", "shot", "subject_position"}
        }
        visual = visuals.get(row["scene_id"])
        scene = {
            "scene_id": row["scene_id"], "episode_id": episode_id,
            "start": round(start, 4), "end": round(end, 4),
            "duration": round(end - start, 4), "beat_ids": beat_ids,
            "speech_start": round(interval_words[0]["start"], 4),
            "speech_end": round(interval_words[-1]["end"], 4),
            "narration_anchor": row["narration_anchor"],
            "resolved_anchor": anchor["matched"],
            "anchor_resolution": anchor["resolution"],
            "visual_type": row["scene_type"], "scene_type": row["scene_type"],
            "character_presence": _character_presence(row, visual),
            "motion": row["motion"],
            "source": "roteiro_visual.csv",
            "timing_source": f"output/audio/{episode_id}/timing.json",
            "timing_quality": "WORD_BOUNDARY_REAL",
            **preserved,
        }
        scenes.append(scene)
    missing = [row["scene_id"] for row in rows if row["scene_id"] not in visuals]
    return {
        "schema_version": "2.0", "episode_id": episode_id,
        "scope": "FULL_EPISODE", "status": "COMPLETE",
        "visual_direction": "FIN_AUDIENCE_PROXY_SITUATIONAL",
        "timing_quality": "WORD_BOUNDARY_REAL", "same_synthesis_audio_timing": True,
        "duration_seconds": round(duration, 4),
        "source": f"episodios/{episode_id}/roteiro_visual.csv",
        "source_sha256": _sha256(visual_script_path),
        "timing_source": f"output/audio/{episode_id}/timing.json",
        "timing_sha256": _sha256(timing_path),
        "narration_source": f"episodios/{episode_id}/roteiro_narracao.md",
        "narration_sha256": _sha256(narration_path),
        "editorial_scene_count": len(rows), "scene_count": len(scenes),
        "visual_scenes_existing": len(visuals),
        "visual_scenes_missing": len(missing), "missing_visual_scene_ids": missing,
        "scenes": scenes,
    }


def write_scene_map(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

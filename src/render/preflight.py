"""Preflight validation for the final episode render.

This module deliberately performs no media generation.  It resolves the
active episode, checks every required input, and returns a structured result
that the render engine may consume only when ``passed`` is true.
"""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from typing import Any

from src.episodes import EpisodeContext, resolve_active_episode


ROOT = Path(__file__).resolve().parents[2]
FRAME_RATE = 30
FRAME_TOLERANCE_SECONDS = (1 / FRAME_RATE) + 0.001
DURATION_TOLERANCE_SECONDS = 0.10
REAL_TIMING_QUALITY = "WORD_BOUNDARY_REAL"
OFFICIAL_NARRATION_SCOPE = "official_narration"

# These are the motion identifiers already accepted by the visual domain.
# Render support remains explicit and fail-closed: unknown identifiers never
# silently fall back to a generic animation.
SUPPORTED_MOTION_PRESETS = frozenset(
    {
        "SLOW_ZOOM_IN",
        "SLOW_ZOOM_OUT",
        "PAN_LEFT",
        "PAN_RIGHT",
        "PAN_UP",
        "PAN_DOWN",
        "STATIC",
        "HOLD",
        "LIGHT_SHAKE",
        "LIGHT_PARALLAX",
        "CROSSFADE",
    }
)

RENDERABLE_VISUAL_STATUSES = frozenset({"GENERATED", "UNKNOWN_BILLED_TIMEOUT"})


def _read_json(path: Path, errors: list[str], label: str) -> Any | None:
    if not path.is_file():
        errors.append(f"{label} not found: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is not valid JSON: {path} ({exc})")
        return None


def _as_path(value: str | Path | None, default: Path) -> Path:
    return Path(value).resolve() if value is not None else default.resolve()


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _resolve_manifest_path(value: Any, *, root: Path, manifest_path: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()

    root_candidate = (root / candidate).resolve()
    if root_candidate.exists():
        return root_candidate
    return (manifest_path.parent / candidate).resolve()


def _wav_duration(path: Path, errors: list[str]) -> float | None:
    if not path.is_file():
        errors.append(f"official narration audio not found: {path}")
        return None
    try:
        with wave.open(str(path), "rb") as audio:
            frame_rate = audio.getframerate()
            if frame_rate <= 0:
                raise ValueError("invalid frame rate")
            return audio.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        errors.append(f"official narration audio is not a readable WAV: {path} ({exc})")
        return None


def _index_by_scene_id(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        return {}
    return {
        str(scene.get("scene_id")): scene
        for scene in scenes
        if isinstance(scene, dict) and scene.get("scene_id")
    }


def _validate_timeline(
    scene_map: Any,
    *,
    errors: list[str],
) -> tuple[list[dict[str, Any]], float | None]:
    if not isinstance(scene_map, dict):
        return [], None
    if scene_map.get("timing_quality") != REAL_TIMING_QUALITY:
        errors.append(f"scene_map timing_quality must be {REAL_TIMING_QUALITY}")

    scenes = scene_map.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scene_map must contain at least one scene")
        return [], None

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_start = 0.0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            errors.append(f"scene_map scene at index {index} must be an object")
            continue
        scene_id = str(scene.get("scene_id") or "").strip()
        if not scene_id:
            errors.append(f"scene_map scene at index {index} has no scene_id")
            continue
        if scene_id in seen_ids:
            errors.append(f"duplicate scene_id in scene_map: {scene_id}")
            continue
        seen_ids.add(scene_id)

        try:
            start = float(scene["start"])
            end = float(scene["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{scene_id}: start/end must be numeric")
            continue

        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            errors.append(f"{scene_id}: invalid interval {start}-{end}")
            continue
        if abs(start - expected_start) > FRAME_TOLERANCE_SECONDS:
            errors.append(
                f"{scene_id}: timeline gap/overlap; expected {expected_start:.3f}, got {start:.3f}"
            )

        motion = str(scene.get("motion") or "").strip()
        if motion not in SUPPORTED_MOTION_PRESETS:
            errors.append(f"{scene_id}: unsupported motion preset: {motion or '<missing>'}")

        normalized.append(
            {
                "scene_id": scene_id,
                "start": start,
                "end": end,
                "duration": end - start,
                "motion_preset": motion,
            }
        )
        expected_start = end

    if not normalized:
        return [], None
    if abs(normalized[0]["start"]) > FRAME_TOLERANCE_SECONDS:
        errors.append("timeline must start at 0.000 seconds")
    return normalized, normalized[-1]["end"]


def run_preflight(
    *,
    root: str | Path = ROOT,
    episode_context: EpisodeContext | None = None,
    narration_path: str | Path | None = None,
    timing_path: str | Path | None = None,
    scene_map_path: str | Path | None = None,
    visual_scenes_path: str | Path | None = None,
    visual_manifest_path: str | Path | None = None,
    motion_contract_path: str | Path | None = None,
    require_production: bool = False,
) -> dict[str, Any]:
    """Validate and resolve all inputs required by a final render."""

    project_root = Path(root).resolve()
    context = episode_context or resolve_active_episode(project_root)
    errors: list[str] = []
    warnings: list[str] = []

    episode_file = context.file("episodio.json").resolve()
    scene_map_file = _as_path(scene_map_path, context.file("scene_map.json"))
    visual_scenes_file = _as_path(visual_scenes_path, context.file("visual_scenes.json"))
    narration_file = _as_path(
        narration_path,
        project_root / "output" / "audio" / context.episode_id / "narration.wav",
    )
    timing_file = _as_path(
        timing_path,
        project_root / "output" / "audio" / context.episode_id / "timing.json",
    )
    visual_manifest_file = _as_path(
        visual_manifest_path,
        project_root / "output" / "generated_images" / "manifest.json",
    )
    motion_contract_file = _as_path(
        motion_contract_path,
        project_root / "config" / "motion_contract.json",
    )

    episode = _read_json(episode_file, errors, "episode config")
    production_stage = episode.get("production_stage") if isinstance(episode, dict) else None
    if require_production and production_stage != "production":
        errors.append(f"production_stage must be production, got {production_stage!r}")
    elif production_stage != "production":
        warnings.append(f"final render is not enabled while production_stage={production_stage}")

    timing = _read_json(timing_file, errors, "official narration timing")
    timing_duration: float | None = None
    if isinstance(timing, dict):
        if timing.get("timing_quality") != REAL_TIMING_QUALITY:
            errors.append(f"narration timing_quality must be {REAL_TIMING_QUALITY}")
        if timing.get("same_synthesis_audio_timing") is not True:
            errors.append("narration timing must come from the same synthesis as the audio")
        if not (
            timing.get("official_narration") is True
            or timing.get("scope") == OFFICIAL_NARRATION_SCOPE
        ):
            errors.append("narration timing is not marked as official_narration")
        try:
            timing_duration = float(timing["duration_seconds"])
            if not math.isfinite(timing_duration) or timing_duration <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append("narration timing has no valid duration_seconds")
            timing_duration = None

    audio_duration = _wav_duration(narration_file, errors)
    scene_map = _read_json(scene_map_file, errors, "scene_map")
    scenes, timeline_duration = _validate_timeline(scene_map, errors=errors)

    visual_scenes = _read_json(visual_scenes_file, errors, "visual_scenes")
    visual_scene_index = _index_by_scene_id(visual_scenes)
    manifest = _read_json(visual_manifest_file, errors, "visual generation manifest")
    manifest_index = _index_by_scene_id(manifest)

    if isinstance(visual_scenes, dict) and not visual_scene_index:
        errors.append("visual_scenes must contain scene metadata")
    if isinstance(manifest, dict) and not manifest_index:
        errors.append("visual generation manifest must contain scene records")

    for scene in scenes:
        scene_id = scene["scene_id"]
        visual_scene = visual_scene_index.get(scene_id)
        if visual_scene is None:
            errors.append(f"{scene_id}: missing visual_scenes metadata")
        else:
            text_policy = visual_scene.get("text_policy")
            mode = text_policy.get("mode") if isinstance(text_policy, dict) else None
            if mode == "OVERLAY":
                errors.append(f"{scene_id}: text overlay is required but not implemented")

        record = manifest_index.get(scene_id)
        if record is None:
            errors.append(f"{scene_id}: missing visual generation record")
            continue
        status = str(record.get("status") or "").upper()
        if status not in RENDERABLE_VISUAL_STATUSES:
            errors.append(f"{scene_id}: visual status is not renderable: {status or '<missing>'}")
        if str(record.get("review_status") or "").upper() != "APPROVED":
            errors.append(f"{scene_id}: visual asset is not APPROVED")
        scene["review_status"] = str(record.get("review_status") or "").upper()

        asset_path = _resolve_manifest_path(
            record.get("final_path"), root=project_root, manifest_path=visual_manifest_file
        )
        if asset_path is None or not asset_path.is_file():
            errors.append(f"{scene_id}: approved final visual asset does not exist")
            continue
        scene["image_path"] = asset_path
        scene["image_source"] = _display_path(asset_path, project_root)

    for scene in scenes:
        if "image_path" not in scene:
            scene["image_path"] = None
            scene["image_source"] = None
        if "review_status" not in scene:
            scene["review_status"] = None

    if timeline_duration is not None and timing_duration is not None:
        if abs(timeline_duration - timing_duration) > DURATION_TOLERANCE_SECONDS:
            errors.append(
                "timeline duration does not match narration timing: "
                f"{timeline_duration:.3f}s vs {timing_duration:.3f}s"
            )
    if timeline_duration is not None and audio_duration is not None:
        if abs(timeline_duration - audio_duration) > DURATION_TOLERANCE_SECONDS:
            errors.append(
                "timeline duration does not match narration audio: "
                f"{timeline_duration:.3f}s vs {audio_duration:.3f}s"
            )

    motion_contract = _read_json(motion_contract_file, errors, "motion contract")
    motion_contract_id = None
    if isinstance(motion_contract, dict):
        motion_contract_id = (
            motion_contract.get("contract_id")
            or motion_contract.get("schema_version")
            or motion_contract_file.name
        )

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "episode_id": context.episode_id,
        "production_stage": production_stage,
        "frame_rate": FRAME_RATE,
        "resolution": {"width": 1920, "height": 1080},
        "timeline_duration": timeline_duration,
        "timing_duration": timing_duration,
        "audio_duration": audio_duration,
        "motion_contract_id": motion_contract_id,
        "scenes": scenes,
        "paths": {
            "episode": episode_file,
            "narration": narration_file,
            "timing": timing_file,
            "scene_map": scene_map_file,
            "visual_scenes": visual_scenes_file,
            "visual_manifest": visual_manifest_file,
            "motion_contract": motion_contract_file,
        },
        "sources": {
            "episode": _display_path(episode_file, project_root),
            "narration": _display_path(narration_file, project_root),
            "timing": _display_path(timing_file, project_root),
            "scene_map": _display_path(scene_map_file, project_root),
            "visual_scenes": _display_path(visual_scenes_file, project_root),
            "visual_manifest": _display_path(visual_manifest_file, project_root),
            "motion_contract": _display_path(motion_contract_file, project_root),
        },
    }

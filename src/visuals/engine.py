"""Carrega e valida o JSON canônico de cenas visuais do episódio."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.episodes import (
    EPISODE_ID_MISMATCH,
    PRODUCTION_STAGES,
    load_json as load_episode_json,
    load_visual_script,
    resolve_active_episode,
    validate_ordered_subset,
    validate_visual_script,
)

from .data_visual import validate_data_visual
from .prompt_builder import CHARACTER_PRESENCES, SCENE_TYPES, TEXT_POLICY_MODES


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VISUAL_SCENES = resolve_active_episode(ROOT).file("visual_scenes.json")
DEFAULT_CHARACTER_LOCK = ROOT / "config" / "character_fin.json"
DEFAULT_GENERATION_CONFIG = ROOT / "config" / "visual_generation.json"
DEFAULT_REFERENCE_PROFILE = ROOT / "config" / "visual_reference_profile.json"
DEFAULT_GENERATED_OUTPUT = ROOT / "output" / "generated_images"

REQUIRED_SCENE_FIELDS = {
    "scene_id",
    "beat_id",
    "scene_type",
    "character_presence",
    "dominant_idea",
    "situation",
    "action",
    "expression",
    "environment",
    "props",
    "framing",
    "lighting",
    "continuity_from",
    "motion",
    "text_policy",
}
OPTIONAL_SCENE_FIELDS = {"data_visual"}
ALLOWED_SCENE_FIELDS = REQUIRED_SCENE_FIELDS | OPTIONAL_SCENE_FIELDS
TEXT_POLICY_ITEM_FIELDS = {"text", "target"}
REQUIRED_ROOT_FIELDS = {"episode_id", "character_lock", "visual_profile", "scenes"}
ALLOWED_FRAMING_FIELDS = {
    "shot",
    "subject_position",
    "camera_angle",
    "fin_occupancy",
}
ALLOWED_SHOTS = {"wide", "medium", "close"}
ALLOWED_POSITIONS = {"left", "center", "right"}
ALLOWED_MOTIONS = {
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


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_generation_config(path: Path = DEFAULT_GENERATION_CONFIG) -> dict[str, Any]:
    return _load_json(path)


def load_reference_profile(path: Path = DEFAULT_REFERENCE_PROFILE) -> dict[str, Any]:
    return _load_json(path)


def load_visual_scenes(path: Path = DEFAULT_VISUAL_SCENES) -> dict[str, Any]:
    payload = _load_json(path)
    context_errors: list[str] = []
    expected_episode_id: str | None = None
    production_stage = "visual_qualification"
    planned_scene_ids: list[str] | None = None
    planned_scene_types: dict[str, str] | None = None
    metadata_path = path.parent / "episodio.json"
    if metadata_path.is_file():
        metadata = load_episode_json(metadata_path)
        expected_episode_id = str(metadata.get("episodio_id", "")).strip() or None
        production_stage = str(metadata.get("production_stage", "")).strip()
        if production_stage not in PRODUCTION_STAGES:
            context_errors.append("unsupported episode production_stage")
    visual_script_path = path.parent / "roteiro_visual.csv"
    if visual_script_path.is_file():
        rows = load_visual_script(visual_script_path)
        context_errors.extend(validate_visual_script(rows))
        planned_scene_ids = [str(row.get("scene_id", "")).strip() for row in rows]
        planned_scene_types = {
            str(row.get("scene_id", "")).strip(): str(row.get("scene_type", "")).strip()
            for row in rows
        }
    errors = context_errors + validate_visual_scenes(
        payload,
        expected_episode_id=expected_episode_id,
        planned_scene_ids=planned_scene_ids,
        planned_scene_types=planned_scene_types,
        production_stage=production_stage,
    )
    if errors:
        raise ValueError("invalid visual_scenes.json: " + "; ".join(errors))
    return payload


def validate_visual_scenes(
    payload: Mapping[str, Any],
    *,
    expected_episode_id: str | None = None,
    planned_scene_ids: Sequence[str] | None = None,
    planned_scene_types: Mapping[str, str] | None = None,
    production_stage: str = "visual_qualification",
) -> list[str]:
    errors: list[str] = []
    root_fields = set(payload)
    if root_fields != REQUIRED_ROOT_FIELDS:
        errors.append(
            "root fields must be exactly " + ", ".join(sorted(REQUIRED_ROOT_FIELDS))
        )
    episode_id = payload.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id.strip():
        errors.append("episode_id must be a non-empty string")
    elif expected_episode_id is not None and episode_id != expected_episode_id:
        errors.append(
            f"{EPISODE_ID_MISMATCH}: visual_scenes.json declares "
            f"{episode_id!r}; active episode is {expected_episode_id!r}"
        )
    if payload.get("character_lock") != "FIN_V1":
        errors.append("character_lock must be FIN_V1")
    if payload.get("visual_profile") != "ILLUSTRATED_V1":
        errors.append("visual_profile must be ILLUSTRATED_V1")

    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        return errors + ["scenes must be a list"]
    scene_ids: list[str] = []
    for index, scene in enumerate(scenes):
        label = scene.get("scene_id", f"index {index}") if isinstance(scene, Mapping) else f"index {index}"
        if not isinstance(scene, Mapping):
            errors.append(f"{label}: scene must be an object")
            continue
        fields = set(scene)
        missing = REQUIRED_SCENE_FIELDS - fields
        extra = fields - ALLOWED_SCENE_FIELDS
        if missing:
            errors.append(f"{label}: missing fields {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"{label}: unsupported fields {', '.join(sorted(extra))}")
        scene_id = str(scene.get("scene_id", ""))
        scene_ids.append(scene_id)
        scene_type = scene.get("scene_type")
        if scene_type not in SCENE_TYPES:
            errors.append(f"{label}: unsupported scene_type")
        elif (
            planned_scene_types is not None
            and scene_id in planned_scene_types
            and scene_type != planned_scene_types[scene_id]
        ):
            errors.append(f"{label}: scene_type differs from roteiro_visual")
        character_presence = scene.get("character_presence")
        if character_presence not in CHARACTER_PRESENCES:
            errors.append(f"{label}: unsupported character_presence")
        if scene_type == "CHARACTER_SCENE" and character_presence != "FIN":
            errors.append(f"{label}: CHARACTER_SCENE requires character_presence FIN")
        if scene_type == "SIMPLE_DATA_SCENE" and character_presence != "NONE":
            errors.append(f"{label}: SIMPLE_DATA_SCENE requires character_presence NONE")
        data_visual = scene.get("data_visual")
        if scene_type == "SIMPLE_DATA_SCENE":
            if "data_visual" not in scene:
                errors.append(f"{label}: SIMPLE_DATA_SCENE requires data_visual")
            else:
                errors.extend(
                    f"{label}: {error}"
                    for error in validate_data_visual(data_visual)
                )
        elif "data_visual" in scene:
            errors.append(f"{label}: data_visual is allowed only for SIMPLE_DATA_SCENE")
        props = scene.get("props")
        if not isinstance(props, list) or len(props) > 3:
            errors.append(f"{label}: props must be a list with at most 3 items")
        framing = scene.get("framing")
        if not isinstance(framing, Mapping):
            errors.append(f"{label}: framing must be an object")
        else:
            framing_extra = set(framing) - ALLOWED_FRAMING_FIELDS
            if framing_extra:
                errors.append(f"{label}: unsupported framing fields")
            if framing.get("shot") not in ALLOWED_SHOTS:
                errors.append(f"{label}: invalid shot")
            if framing.get("subject_position") not in ALLOWED_POSITIONS:
                errors.append(f"{label}: invalid subject_position")
            occupancy = framing.get("fin_occupancy")
            if occupancy is not None and not 0 <= float(occupancy) <= 1:
                errors.append(f"{label}: fin_occupancy must be between 0 and 1")
        motion = scene.get("motion")
        if not isinstance(motion, Mapping) or set(motion) != {"preset"}:
            errors.append(f"{label}: motion must contain only preset")
        elif motion.get("preset") not in ALLOWED_MOTIONS:
            errors.append(f"{label}: invalid motion preset")
        text_policy = scene.get("text_policy")
        if not isinstance(text_policy, Mapping):
            errors.append(f"{label}: text_policy must be an object")
            continue
        if set(text_policy) != {"mode", "items"}:
            errors.append(f"{label}: text_policy must contain only mode and items")
            continue
        mode = text_policy.get("mode")
        items = text_policy.get("items")
        if mode not in TEXT_POLICY_MODES:
            errors.append(f"{label}: invalid text_policy mode")
        if not isinstance(items, list):
            errors.append(f"{label}: text_policy items must be a list")
            continue
        if mode == "NONE" and items:
            errors.append(f"{label}: NONE text_policy cannot declare items")
        if mode == "OVERLAY" and not items:
            errors.append(f"{label}: OVERLAY text_policy must declare items")
        if scene_type == "SIMPLE_DATA_SCENE" and (mode != "NONE" or items):
            errors.append(f"{label}: SIMPLE_DATA_SCENE requires text_policy NONE")
        for item_index, item in enumerate(items):
            item_label = f"{label}: text_policy item {item_index}"
            if not isinstance(item, Mapping) or set(item) != TEXT_POLICY_ITEM_FIELDS:
                errors.append(f"{item_label} must contain only text and target")
                continue
            for field in TEXT_POLICY_ITEM_FIELDS:
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{item_label}: {field} must be a non-empty string")
                elif "\n" in value or "\r" in value:
                    errors.append(f"{item_label}: {field} must be single-line")

    if any(not scene_id for scene_id in scene_ids):
        errors.append("scene_id must not be empty")
    if len(scene_ids) != len(set(scene_ids)):
        errors.append("scene_ids must be unique")
    if planned_scene_ids is not None:
        errors.extend(
            validate_ordered_subset(
                scene_ids,
                planned_scene_ids,
                label="visual_scenes",
                production_stage=production_stage,
            )
        )
    elif production_stage not in PRODUCTION_STAGES:
        errors.append("unsupported production_stage")
    return errors


def build_generation_jobs(
    scene_ids: Sequence[str] | None = None,
    *,
    visual_scenes_path: Path = DEFAULT_VISUAL_SCENES,
) -> list[dict[str, Any]]:
    """Seleciona cenas do JSON canônico sem compilar ou persistir prompts."""

    payload = load_visual_scenes(visual_scenes_path)
    by_id = {scene["scene_id"]: scene for scene in payload["scenes"]}
    selected = list(scene_ids) if scene_ids is not None else list(by_id)
    if not selected:
        raise ValueError("at least one scene is required")
    missing = [scene_id for scene_id in selected if scene_id not in by_id]
    if missing:
        raise ValueError("unknown visual scenes: " + ", ".join(missing))
    return [deepcopy(by_id[scene_id]) for scene_id in selected]

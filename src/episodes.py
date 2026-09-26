"""Resolucao do episodio ativo e validacoes dinamicas de sequencia."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_CONFIG = ROOT / "config" / "project.json"
PRODUCTION_STAGES = ("visual_qualification", "production")
VISUAL_SCENE_TYPES = (
    "CHARACTER_SCENE",
    "OBJECT_SCENE",
    "ENVIRONMENT_SCENE",
    "SIMPLE_DATA_SCENE",
)
VISUAL_FORMATS = ("image",)
OFFICIAL_VISUAL_DIRECTION = "FIN_AUDIENCE_PROXY_SITUATIONAL"
REAL_TIMING_QUALITY = "WORD_BOUNDARY_REAL"
SCENE_ID_PATTERN = re.compile(r"S(\d+)")


class EpisodeConfigError(ValueError):
    """Configuracao de projeto ou episodio invalida."""


@dataclass(frozen=True)
class EpisodeContext:
    root: Path
    episode_id: str
    episode_dir: Path
    production_stage: str
    project: Mapping[str, Any]
    metadata: Mapping[str, Any]

    def file(self, name: str) -> Path:
        return self.episode_dir / name


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_active_episode(
    root: Path = ROOT,
    project_config_path: Path | None = None,
) -> EpisodeContext:
    """Resolve uma unica vez o diretorio do episodio ativo pelo project.json."""

    resolved_root = root.resolve()
    config_path = project_config_path or resolved_root / "config" / "project.json"
    project = load_json(config_path)
    episode_id = str(project.get("active_episode", "")).strip()
    if not episode_id or Path(episode_id).name != episode_id or episode_id in {".", ".."}:
        raise EpisodeConfigError("project active_episode must be a safe directory name")
    episode_dir = (resolved_root / "episodios" / episode_id).resolve()
    if episode_dir.parent != (resolved_root / "episodios").resolve():
        raise EpisodeConfigError("active episode resolves outside episodios")
    metadata_path = episode_dir / "episodio.json"
    if not metadata_path.is_file():
        raise EpisodeConfigError(f"active episode metadata is missing: {metadata_path}")
    metadata = load_json(metadata_path)
    metadata_episode_id = str(metadata.get("episodio_id", "")).strip()
    if metadata_episode_id != episode_id:
        raise EpisodeConfigError("episodio.json id must match project active_episode")
    production_stage = str(metadata.get("production_stage", "")).strip()
    if production_stage not in PRODUCTION_STAGES:
        raise EpisodeConfigError(
            "episodio.json production_stage must be visual_qualification or production"
        )
    return EpisodeContext(
        root=resolved_root,
        episode_id=episode_id,
        episode_dir=episode_dir,
        production_stage=production_stage,
        project=project,
        metadata=metadata,
    )


def load_visual_script(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_time(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ValueError("timestamp must be non-negative")
        return seconds
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp is empty")
    if ":" not in text:
        seconds = float(text)
        if seconds < 0:
            raise ValueError("timestamp must be non-negative")
        return seconds
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("timestamp must use MM:SS or HH:MM:SS")
    numbers = [float(part) for part in parts]
    if any(number < 0 for number in numbers):
        raise ValueError("timestamp must be non-negative")
    if numbers[-1] >= 60 or (len(numbers) == 3 and numbers[-2] >= 60):
        raise ValueError("timestamp minutes or seconds are out of range")
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def _sequence_errors(
    scene_ids: Sequence[str],
    label: str,
    *,
    require_sequential: bool,
) -> list[str]:
    errors: list[str] = []
    if any(not scene_id for scene_id in scene_ids):
        errors.append(f"{label}: scene_id must not be empty")
    duplicates = sorted(
        {scene_id for scene_id in scene_ids if scene_ids.count(scene_id) > 1}
    )
    if duplicates:
        errors.append(f"{label}: duplicate scene_ids: {', '.join(duplicates)}")
    if require_sequential:
        matches = [SCENE_ID_PATTERN.fullmatch(scene_id) for scene_id in scene_ids]
        if any(matches) and not all(matches):
            errors.append(f"{label}: SNNN scene_ids cannot be mixed with another id pattern")
        elif matches and all(matches):
            numbers = [int(match.group(1)) for match in matches if match is not None]
            expected = list(range(numbers[0], numbers[0] + len(numbers)))
            if numbers != expected:
                errors.append(f"{label}: SNNN scene_ids must be sequential and ordered")
    return errors


def validate_visual_script(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Valida o roteiro visual sem assumir quantidade ou duracao fixa."""

    errors: list[str] = []
    if not rows:
        return ["roteiro_visual: at least one scene is required"]
    scene_ids = [str(row.get("scene_id", "")).strip() for row in rows]
    errors.extend(
        _sequence_errors(scene_ids, "roteiro_visual", require_sequential=True)
    )
    previous_end: float | None = None
    for index, row in enumerate(rows):
        label = scene_ids[index] or f"row {index + 1}"
        scene_type = str(row.get("scene_type", "")).strip()
        if scene_type not in VISUAL_SCENE_TYPES:
            errors.append(f"{label}: unsupported scene_type")
        visual_format = str(row.get("format", "")).strip()
        if visual_format not in VISUAL_FORMATS:
            errors.append(f"{label}: unsupported format")
        start_raw = row.get("start")
        end_raw = row.get("end")
        has_start = start_raw is not None and str(start_raw).strip() != ""
        has_end = end_raw is not None and str(end_raw).strip() != ""
        if has_start != has_end:
            errors.append(f"{label}: start and end must be declared together")
            continue
        if not has_start:
            continue
        try:
            start = _parse_time(start_raw)
            end = _parse_time(end_raw)
        except (TypeError, ValueError):
            errors.append(f"{label}: invalid start or end timestamp")
            continue
        if end <= start:
            errors.append(f"{label}: end must be greater than start")
        if previous_end is not None and start < previous_end:
            errors.append(f"{label}: scene timing overlaps the previous timed scene")
        previous_end = end
    return errors


def validate_ordered_subset(
    scene_ids: Sequence[str],
    planned_scene_ids: Sequence[str],
    *,
    label: str,
    production_stage: str,
) -> list[str]:
    """Valida pertencimento, ordem relativa e cobertura derivada do roteiro."""

    errors = _sequence_errors(scene_ids, label, require_sequential=False)
    if production_stage not in PRODUCTION_STAGES:
        errors.append(f"{label}: unsupported production_stage")
        return errors
    positions = {scene_id: index for index, scene_id in enumerate(planned_scene_ids)}
    unknown = [scene_id for scene_id in scene_ids if scene_id not in positions]
    if unknown:
        errors.append(f"{label}: scenes absent from roteiro_visual: {', '.join(unknown)}")
    known_positions = [positions[scene_id] for scene_id in scene_ids if scene_id in positions]
    if known_positions != sorted(known_positions):
        errors.append(f"{label}: relative order differs from roteiro_visual")
    if production_stage == "production" and list(scene_ids) != list(planned_scene_ids):
        errors.append(f"{label}: production coverage must match roteiro_visual")
    return errors


def validate_scene_map(
    payload: Mapping[str, Any],
    *,
    expected_episode_id: str,
    planned_scene_ids: Sequence[str],
    planned_scene_types: Mapping[str, str] | None = None,
    production_stage: str,
    expected_timing_quality: str | None = None,
) -> list[str]:
    """Valida mapa temporal completo ou parcial a partir do roteiro planejado."""

    errors: list[str] = []
    if payload.get("episode_id") != expected_episode_id:
        errors.append("scene_map: episode_id does not match active episode")
    if payload.get("visual_direction") != OFFICIAL_VISUAL_DIRECTION:
        errors.append("scene_map: visual_direction is not canonical")
    timing_quality = payload.get("timing_quality")
    if timing_quality not in {None, "", REAL_TIMING_QUALITY}:
        errors.append("scene_map: unsupported timing_quality")
    if timing_quality and expected_timing_quality and timing_quality != expected_timing_quality:
        errors.append("scene_map: timing_quality differs from project config")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        return errors + ["scene_map: scenes must be a list"]
    scene_ids = [
        str(scene.get("scene_id", "")).strip() if isinstance(scene, Mapping) else ""
        for scene in scenes
    ]
    errors.extend(
        validate_ordered_subset(
            scene_ids,
            planned_scene_ids,
            label="scene_map",
            production_stage=production_stage,
        )
    )
    previous_start: float | None = None
    previous_end: float | None = None
    for index, scene in enumerate(scenes):
        label = scene_ids[index] or f"scene {index + 1}"
        if not isinstance(scene, Mapping):
            errors.append(f"{label}: scene must be an object")
            continue
        if scene.get("scene_type") not in VISUAL_SCENE_TYPES:
            errors.append(f"{label}: unsupported scene_type")
        elif (
            planned_scene_types is not None
            and label in planned_scene_types
            and scene.get("scene_type") != planned_scene_types[label]
        ):
            errors.append(f"{label}: scene_type differs from roteiro_visual")
        if not scene.get("visual_intent") or not scene.get("setting"):
            errors.append(f"{label}: visual_intent and setting are required")
        try:
            start = _parse_time(scene.get("start"))
            end = _parse_time(scene.get("end"))
        except (TypeError, ValueError):
            errors.append(f"{label}: invalid start or end timestamp")
            continue
        if end <= start:
            errors.append(f"{label}: duration must be positive")
        if previous_start is not None and start < previous_start:
            errors.append(f"{label}: scene_map must be in increasing temporal order")
        if previous_end is not None and start < previous_end:
            errors.append(f"{label}: scene_map timing overlaps the previous scene")
        previous_start = start
        previous_end = end
    return errors

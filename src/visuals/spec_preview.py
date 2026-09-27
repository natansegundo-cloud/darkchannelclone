"""Offline coverage, prompt, reference, routing, and cost preview for visual specs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from src.episodes import load_json, load_visual_script

from .engine import (
    build_generation_jobs,
    load_generation_config,
    load_reference_profile,
)
from .prompt_builder import build_prompt
from .references import resolve_scene_references
from .routing import DETERMINISTIC_LOCAL_RENDERER, resolve_visual_route
from .validators import validate_compiled_prompt


def _route_name(renderer: str, model: str) -> str:
    if renderer == DETERMINISTIC_LOCAL_RENDERER:
        return "LOCAL_DETERMINISTIC"
    if "flux" in model.casefold():
        return "FLUX"
    if "gpt-image" in model.casefold():
        return "GPT_IMAGE"
    return model


def _has_output(output_dir: Path, scene_id: str) -> bool:
    return any((output_dir / folder / f"{scene_id}.png").is_file() for folder in ("", "drafts", "final"))


def preview_visual_specs(
    *, visual_scenes_path: Path, visual_script_path: Path, scene_map_path: Path,
    output_dir: Path, cost_scene_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    rows = load_visual_script(visual_script_path)
    scene_map = load_json(scene_map_path)
    jobs = build_generation_jobs(visual_scenes_path=visual_scenes_path)
    editorial_ids = [str(row["scene_id"]) for row in rows]
    map_ids = [str(scene["scene_id"]) for scene in scene_map.get("scenes", [])]
    visual_ids = [str(scene["scene_id"]) for scene in jobs]
    if editorial_ids != map_ids or editorial_ids != visual_ids:
        raise ValueError("editorial, scene map, and visual scene coverage differ")

    config = load_generation_config()
    profile = load_reference_profile()
    routes: Counter[str] = Counter()
    prompt_count = 0
    generative_missing = 0
    deterministic_missing = 0
    route_by_scene: dict[str, Any] = {}
    for scene in jobs:
        prompt = build_prompt(scene)
        prompt_errors = validate_compiled_prompt(prompt)
        if prompt_errors or not prompt.strip():
            raise ValueError(f"{scene['scene_id']}: invalid compiled prompt: {'; '.join(prompt_errors)}")
        prompt_count += 1
        references = resolve_scene_references(scene, profile)
        required_fin = set(profile["required_references"])
        if scene["character_presence"] == "NONE" and required_fin.intersection(references):
            raise ValueError(f"{scene['scene_id']}: NONE scene received FIN references")
        if scene["character_presence"] == "FIN" and not required_fin.issubset(references):
            raise ValueError(f"{scene['scene_id']}: FIN scene is missing character references")
        route = resolve_visual_route(scene, config)
        name = _route_name(route.renderer, route.model)
        routes[name] += 1
        route_by_scene[scene["scene_id"]] = route
        if not _has_output(output_dir, scene["scene_id"]):
            if route.api_required:
                generative_missing += 1
            else:
                deterministic_missing += 1

    priced_ids = list(cost_scene_ids) if cost_scene_ids is not None else visual_ids
    unknown = sorted(set(priced_ids) - set(visual_ids))
    if unknown:
        raise ValueError("cost preview contains unknown scenes: " + ", ".join(unknown))
    estimated_cost = sum(float(route_by_scene[scene_id].cost_usd) for scene_id in priced_ids)
    return {
        "editorial_scenes": len(editorial_ids), "scene_map_scenes": len(map_ids),
        "visual_scenes": len(visual_ids), "prompt_compilation": "PASS",
        "compiled_prompts": prompt_count, "routing_preview": dict(routes),
        "cost_scene_count": len(priced_ids),
        "estimated_generation_cost_usd": round(estimated_cost, 4),
        "generative_scenes_missing_output": generative_missing,
        "deterministic_scenes_missing_output": deterministic_missing,
        "external_calls": 0,
    }

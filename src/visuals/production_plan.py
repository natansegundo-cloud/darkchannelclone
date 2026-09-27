"""Validation for the offline visual asset production/reuse plan."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


PRODUCTION_MODES = {
    "EXISTING_ASSET", "NEW_GENERATIVE_BASE", "REUSE_GENERATIVE_BASE",
    "LOCAL_REFRAME", "LOCAL_OVERLAY", "LOCAL_DETERMINISTIC",
}
DERIVATION_MODES = {"NONE", "CROP", "PAN", "ZOOM", "OVERLAY", "REFRAME", "DETERMINISTIC_CHANGE"}
REUSE_RISKS = {"LOW", "MEDIUM", "HIGH"}


def validate_visual_production_plan(
    payload: Mapping[str, Any], *, episode_id: str, planned_scene_ids: Sequence[str],
    generation_config: Mapping[str, Any], project_root: Path,
) -> list[str]:
    errors: list[str] = []
    if payload.get("episode_id") != episode_id:
        errors.append("visual production plan episode_id mismatch")
    if set(payload.get("production_modes", [])) != PRODUCTION_MODES:
        errors.append("visual production plan has non-canonical production modes")
    if set(payload.get("derivation_modes", [])) != DERIVATION_MODES:
        errors.append("visual production plan has non-canonical derivation modes")
    sequences = payload.get("sequences")
    if not isinstance(sequences, list) or not sequences:
        return errors + ["visual production plan must contain sequences"]
    sequence_ids: list[str] = []
    assigned: list[str] = []
    derivations: list[Mapping[str, Any]] = []
    known_bases = set(planned_scene_ids)
    for sequence in sequences:
        if not isinstance(sequence, Mapping):
            errors.append("visual production sequence must be an object")
            continue
        sequence_id = str(sequence.get("sequence_id") or "")
        sequence_ids.append(sequence_id)
        scene_ids = sequence.get("scene_ids")
        rows = sequence.get("derivations")
        if not isinstance(scene_ids, list) or not isinstance(rows, list):
            errors.append(f"{sequence_id}: scene_ids and derivations must be lists")
            continue
        row_ids = [str(row.get("scene_id") or "") for row in rows if isinstance(row, Mapping)]
        if row_ids != scene_ids:
            errors.append(f"{sequence_id}: derivations must match sequence scene_ids")
        assigned.extend(str(item) for item in scene_ids)
        derivations.extend(row for row in rows if isinstance(row, Mapping))
        if sequence.get("base_source") not in {"EXISTING", "NEW"}:
            errors.append(f"{sequence_id}: invalid base_source")
        if sequence.get("base_scene") not in known_bases:
            errors.append(f"{sequence_id}: unknown base_scene")
        if sequence.get("reuse_risk") not in REUSE_RISKS:
            errors.append(f"{sequence_id}: invalid reuse risk")
        try:
            if float(sequence.get("estimated_cost")) < 0:
                errors.append(f"{sequence_id}: negative cost")
        except (TypeError, ValueError):
            errors.append(f"{sequence_id}: invalid cost")
        for row in rows:
            if row.get("production_mode") not in PRODUCTION_MODES:
                errors.append(f"{row.get('scene_id')}: invalid production mode")
            if row.get("derivation") not in DERIVATION_MODES:
                errors.append(f"{row.get('scene_id')}: invalid derivation mode")
    if len(sequence_ids) != len(set(sequence_ids)):
        errors.append("visual production plan has duplicate sequence_id")
    if assigned != list(planned_scene_ids):
        errors.append("visual production plan coverage/order differs from editorial scenes")
    if len(assigned) != len(set(assigned)):
        errors.append("visual production plan assigns a scene more than once")

    declared_assets = payload.get("approved_assets", [])
    approved_ids = set()
    for asset in declared_assets if isinstance(declared_assets, list) else []:
        if not isinstance(asset, Mapping):
            errors.append("approved asset entry must be an object")
            continue
        approved_ids.add(asset.get("scene_id"))
        path = project_root / str(asset.get("path") or "")
        if not path.is_file() or asset.get("status") not in {"APPROVED", "APPROVED_REFERENCE"}:
            errors.append(f"{asset.get('scene_id')}: approved asset is unavailable or unapproved")
    for sequence in sequences:
        if sequence.get("base_source") == "EXISTING" and sequence.get("base_scene") not in approved_ids:
            errors.append(f"{sequence.get('sequence_id')}: existing base is not approved")

    mode_counts = Counter(str(row.get("production_mode")) for row in derivations)
    new_flux = sum(
        row.get("production_mode") == "NEW_GENERATIVE_BASE" and sequence.get("new_provider") == "FLUX"
        for sequence in sequences for row in sequence.get("derivations", [])
    )
    new_gpt = sum(
        row.get("production_mode") == "NEW_GENERATIVE_BASE" and sequence.get("new_provider") == "GPT_IMAGE"
        for sequence in sequences for row in sequence.get("derivations", [])
    )
    flux_cost = float(generation_config["tiers"]["cheap_draft"]["estimated_cost_per_image"])
    gpt_cost = float(generation_config["tiers"]["mid_fallback"]["estimated_cost_per_image"])
    calculated_cost = round(new_flux * flux_cost + new_gpt * gpt_cost, 4)
    optimized = payload.get("cost_model", {}).get("optimized_plan", {})
    if optimized.get("paid_generations") != new_flux + new_gpt:
        errors.append("optimized paid generation count is inconsistent")
    if float(optimized.get("estimated_cost", -1)) != calculated_cost:
        errors.append("optimized cost is inconsistent with local config")
    summary = payload.get("summary", {})
    expected_summary = {
        "total_scenes": len(planned_scene_ids),
        "visual_sequence_count": len(sequences),
        "new_flux_bases": new_flux,
        "new_gpt_bases": new_gpt,
        "existing_asset_scenes": mode_counts["EXISTING_ASSET"],
        "existing_asset_reuses": mode_counts["LOCAL_REFRAME"],
        "new_base_reuses": mode_counts["REUSE_GENERATIVE_BASE"],
        "local_reframes": mode_counts["LOCAL_REFRAME"],
        "local_overlays": mode_counts["LOCAL_OVERLAY"],
        "local_deterministic": mode_counts["LOCAL_DETERMINISTIC"],
        "unassigned_scenes": 0,
        "external_calls": 0,
    }
    for field, expected in expected_summary.items():
        if summary.get(field) != expected:
            errors.append(f"visual production summary {field} is inconsistent")
    usage = Counter(str(sequence.get("base_scene")) for sequence in sequences)
    if summary.get("max_scenes_from_one_base") != max(usage.values(), default=0):
        errors.append("max_scenes_from_one_base is inconsistent")
    high = [scene_id for sequence in sequences if sequence.get("reuse_risk") == "HIGH" for scene_id in sequence.get("scene_ids", [])]
    if summary.get("high_reuse_risk_scenes") != high:
        errors.append("high reuse risk scene list is inconsistent")
    return errors

"""Validações objetivas da configuração e dos outputs visuais."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


TIER_NAMES = ("cheap_draft", "mid_fallback", "premium_optional")
REVIEW_STATUSES = ("PENDING", "APPROVED", "UPGRADE_REQUESTED", "REJECTED")
UNKNOWN_BILLED_TIMEOUT = "UNKNOWN_BILLED_TIMEOUT"
SCENE_STATUSES = (
    "pending",
    "dry_run",
    "generated",
    "failed",
    "skipped_budget",
    UNKNOWN_BILLED_TIMEOUT,
)
TIER_FIELDS = (
    "enabled",
    "provider",
    "model",
    "max_attempts",
    "timeout",
    "estimated_cost_per_image",
)
SCENE_MANIFEST_FIELDS = {
    "scene_id",
    "status",
    "review_status",
    "selected_tier",
    "provider",
    "model",
    "attempts",
    "cost_usd",
    "references_used",
    "output_path",
    "draft_path",
    "final_path",
    "error",
    "prompt_hash",
}


def validate_compiled_prompt(prompt: str) -> list[str]:
    errors: list[str] = []
    if not prompt.strip():
        errors.append("compiled prompt is empty")
    if "\n" in prompt or "\r" in prompt:
        errors.append("compiled prompt contains CR or LF")
    for required in (
        "ILLUSTRATED_V1",
        "ILLUSTRATED_V1 STYLE LOCK.",
        "NOT PHOTOREALISTIC.",
        "NO PHOTOGRAPHY.",
        "NO 3D RENDER.",
        "NEGATIVE RULES:",
        "TEXT POLICY:",
        "NO UNDECLARED TEXT.",
        "SYSTEM DECIDES CONTENT. AI DECIDES APPEARANCE.",
        "Do not introduce undeclared narrative elements",
    ):
        if required not in prompt:
            errors.append(f"compiled prompt does not resolve {required}")
    return errors


def validate_generation_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in (
        "default_provider",
        "default_visual_profile",
        "quality_first",
        "auto_quality_upgrade",
        "generation_principle",
        "decision_priority",
        "default_budget_usd",
        "max_retries_per_scene",
        "dry_run",
        "resume",
        "require_single_line_prompt",
        "tiers",
        "fallback_policy",
        "scene_type_routing",
    ):
        if field not in config:
            errors.append(f"visual generation config missing {field}")
    tiers = config.get("tiers", {})
    for tier_name in TIER_NAMES:
        tier = tiers.get(tier_name)
        if not isinstance(tier, Mapping):
            errors.append(f"visual generation config missing tier {tier_name}")
            continue
        for field in TIER_FIELDS:
            if field not in tier:
                errors.append(f"tier {tier_name} missing {field}")
    expected = {
        "first_tier": "cheap_draft",
        "second_tier": "mid_fallback",
        "third_tier": "premium_optional",
    }
    if config.get("fallback_policy") != expected:
        errors.append("fallback policy must be cheap_draft -> mid_fallback -> premium_optional")
    if config.get("default_provider") != "openrouter":
        errors.append("default provider must be openrouter")
    if config.get("quality_first") is not True:
        errors.append("quality_first must be true")
    if config.get("auto_quality_upgrade") is not False:
        errors.append("auto_quality_upgrade must be false")
    if config.get("generation_principle") != "USE GENERATIVE AI ONLY WHEN GENERATIVE AI ADDS VALUE.":
        errors.append("visual generation principle is incorrect")
    expected_priority = [
        "narrative_adherence",
        "FIN_V1_consistency",
        "visual_quality",
        "continuity",
        "cost",
    ]
    if config.get("decision_priority") != expected_priority:
        errors.append("visual decision priority is incorrect")
    if config.get("default_budget_usd") != 0.15:
        errors.append("default budget must be US$0.15")
    if config.get("max_retries_per_scene") != 1:
        errors.append("max_retries_per_scene must be 1")
    expected_tiers = {
        "cheap_draft": (True, "openrouter", "black-forest-labs/flux.2-klein-4b", 120),
        "mid_fallback": (True, "openrouter", "openai/gpt-image-2", 180),
        "premium_optional": (False, "openrouter", "openai/gpt-image-2", 180),
    }
    for tier_name, (enabled, provider, model, timeout) in expected_tiers.items():
        tier = tiers.get(tier_name, {})
        if tier.get("enabled") is not enabled:
            errors.append(f"tier {tier_name} enabled value is incorrect")
        if tier.get("provider") != provider or tier.get("model") != model:
            errors.append(f"tier {tier_name} provider/model is incorrect")
        if tier.get("max_attempts") != 1:
            errors.append(f"tier {tier_name} must allow one attempt")
        if tier.get("timeout") != timeout:
            errors.append(f"tier {tier_name} timeout must be {timeout} seconds")
    if float(config.get("default_budget_usd", -1)) < 0:
        errors.append("default budget must be non-negative")
    if config.get("require_single_line_prompt") is not True:
        errors.append("require_single_line_prompt must be true")
    expected_routing = {
        "source": "SCENE_TYPE",
        "routes": {
            "CHARACTER_SCENE": {
                "renderer": "generative",
                "tier": "cheap_draft",
                "allow_fallback": True,
            },
            "ENVIRONMENT_SCENE": {
                "renderer": "generative",
                "tier": "cheap_draft",
                "allow_fallback": True,
            },
            "OBJECT_SCENE": {
                "renderer": "generative",
                "tier": "mid_fallback",
                "allow_fallback": False,
                "with_character_route": "CHARACTER_SCENE",
            },
            "SIMPLE_DATA_SCENE": {
                "renderer": "deterministic_local",
                "provider": "local",
                "model": "deterministic_local",
                "cost_usd": 0.0,
            },
        },
    }
    if config.get("scene_type_routing") != expected_routing:
        errors.append("scene_type routing policy is incorrect")
    return errors


def validate_reference_profile(profile: Mapping[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    if profile.get("profile_id") != "ILLUSTRATED_V1":
        errors.append("visual reference profile must be ILLUSTRATED_V1")
    if profile.get("fin_lock") != "FIN_V1":
        errors.append("visual reference profile must lock FIN_V1")
    references = list(profile.get("required_references", []))
    scene_references = profile.get("scene_reference_images", {})
    if not isinstance(scene_references, Mapping):
        errors.append("scene_reference_images must be an object")
        scene_references = {}
    references.extend(scene_references.values())
    compatibility = profile.get("scene_reference_character_presence", {})
    if not isinstance(compatibility, Mapping):
        errors.append("scene reference character presence must be an object")
    else:
        unknown_compatibility = set(compatibility) - set(scene_references)
        if unknown_compatibility:
            errors.append("scene reference compatibility has no configured image")
        if any(value not in {"FIN", "NONE"} for value in compatibility.values()):
            errors.append("scene reference character presence must be FIN or NONE")
    for reference in references:
        if not (root / reference).is_file():
            errors.append(f"missing visual reference: {reference}")
    if not profile.get("style_rules"):
        errors.append("visual reference profile has no style rules")
    if not profile.get("negative_rules"):
        errors.append("visual reference profile has no negative rules")
    return errors


def validate_generated_image(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    png_signature = exists and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    errors = []
    if not exists:
        errors.append("image file is missing")
    elif size == 0:
        errors.append("image file is empty")
    elif not png_signature:
        errors.append("image file is not a PNG")
    return {
        "passed": not errors,
        "checks": {
            "exists": exists,
            "non_empty": size > 0,
            "png_signature": png_signature,
        },
        "errors": errors,
    }


def validate_output(output_dir: Path, root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = output_dir / "manifest.json"
    summary_path = output_dir / "summary.md"
    review_index_path = output_dir / "review_index.html"
    if not manifest_path.is_file():
        return [f"missing output manifest: {manifest_path}"]
    if not summary_path.is_file():
        errors.append(f"missing output summary: {summary_path}")
    if not review_index_path.is_file():
        errors.append(f"missing review index: {review_index_path}")
    if list(output_dir.rglob("*.txt")):
        errors.append("generated output must not contain prompt TXT artifacts")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for scene in manifest.get("scenes", []):
        scene_id = scene.get("scene_id", "UNKNOWN")
        fields = set(scene)
        if fields != SCENE_MANIFEST_FIELDS:
            errors.append(f"{scene_id}: manifest scene fields are not canonical")
        if scene.get("review_status") not in REVIEW_STATUSES:
            errors.append(f"{scene_id}: invalid review_status")
        if scene.get("status") not in SCENE_STATUSES:
            errors.append(f"{scene_id}: invalid status")
        prompt_hash = str(scene.get("prompt_hash", ""))
        if len(prompt_hash) != 64:
            errors.append(f"{scene_id}: invalid prompt_hash")
        for reference in scene.get("references_used", []):
            if not (root / reference).is_file():
                errors.append(f"{scene_id}: missing reference {reference}")
        if scene.get("status") == "generated":
            draft_path = scene.get("draft_path")
            draft_image = Path(str(draft_path)) if draft_path else None
            if draft_image is None:
                errors.append(f"{scene_id}: generated scene has no draft_path")
            else:
                if not draft_image.is_absolute():
                    draft_image = root / draft_image
                result = validate_generated_image(draft_image)
                errors.extend(f"{scene_id}: {error}" for error in result["errors"])
            final_path = scene.get("final_path")
            if final_path:
                final_image = Path(str(final_path))
                if not final_image.is_absolute():
                    final_image = root / final_image
                result = validate_generated_image(final_image)
                errors.extend(f"{scene_id}: {error}" for error in result["errors"])
            if scene.get("review_status") == "APPROVED" and not final_path:
                errors.append(f"{scene_id}: approved scene has no final_path")
        if scene.get("status") == UNKNOWN_BILLED_TIMEOUT:
            if scene.get("review_status") not in {"PENDING", "APPROVED"}:
                errors.append(f"{scene_id}: billed timeout review status is invalid")
            if scene.get("error") != "TimeoutError":
                errors.append(f"{scene_id}: billed timeout must record TimeoutError")
            if scene.get("output_path") is not None:
                errors.append(f"{scene_id}: billed timeout cannot have output_path")
            draft_path = scene.get("draft_path")
            if draft_path:
                draft_image = Path(str(draft_path))
                if not draft_image.is_absolute():
                    draft_image = root / draft_image
                result = validate_generated_image(draft_image)
                errors.extend(f"{scene_id}: {error}" for error in result["errors"])
            if scene.get("review_status") == "APPROVED":
                final_path = scene.get("final_path")
                if not final_path:
                    errors.append(f"{scene_id}: approved billed timeout has no final_path")
                else:
                    final_image = Path(str(final_path))
                    if not final_image.is_absolute():
                        final_image = root / final_image
                    result = validate_generated_image(final_image)
                    errors.extend(f"{scene_id}: {error}" for error in result["errors"])
    return errors

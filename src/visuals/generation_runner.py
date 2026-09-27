"""Executa cenas JSON em tiers e mantém prompts compilados somente em memória."""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from src.episodes import EPISODE_ID_MISMATCH, resolve_active_episode

from .benchmark import (
    DEFAULT_BENCHMARK_OUTPUT,
    configure_api_key_environment,
    run_benchmark,
)
from .budget_manager import BudgetManager
from .deterministic_renderer import (
    RENDERER_ID as DETERMINISTIC_RENDERER_ID,
    RENDERER_VERSION as DETERMINISTIC_RENDERER_VERSION,
    render_deterministic_scene,
)
from .engine import (
    DEFAULT_GENERATED_OUTPUT,
    DEFAULT_VISUAL_SCENES,
    ROOT,
    build_generation_jobs,
    load_generation_config,
    load_reference_profile,
    load_visual_scenes,
    validate_visual_scenes,
)
from .prompt_builder import build_prompt
from .providers import GenerationRequest, ProviderRegistry, UNKNOWN_BILLED_TIMEOUT
from .references import resolve_scene_references
from .routing import (
    DETERMINISTIC_LOCAL_RENDERER,
    resolve_visual_route,
    routed_tier_names,
)
from .scene_map import SceneMapError, build_scene_map, write_scene_map
from .spec_preview import preview_visual_specs
from .validators import (
    validate_compiled_prompt,
    validate_generated_image,
    validate_generation_config,
    validate_output,
    validate_reference_profile,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def _generation_lock(output_dir: Path, *, dry_run: bool, run_id: str):
    if dry_run:
        yield
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "generation.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"generation lock already exists: {lock_path}; verify no generation process is running"
        ) from exc
    try:
        os.write(
            descriptor,
            json.dumps({"pid": os.getpid(), "run_id": run_id}).encode("utf-8"),
        )
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _tier_order(config: Mapping[str, Any]) -> tuple[str, str, str]:
    policy = config["fallback_policy"]
    return (
        str(policy["first_tier"]),
        str(policy["second_tier"]),
        str(policy["third_tier"]),
    )


def _enabled_tiers(
    config: Mapping[str, Any],
    *,
    enable_premium: bool,
    allowed_tiers: Sequence[str] | None = None,
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    allowed = set(allowed_tiers) if allowed_tiers is not None else None
    for tier_name in _tier_order(config):
        if allowed is not None and tier_name not in allowed:
            continue
        tier = config["tiers"][tier_name]
        enabled = bool(tier["enabled"])
        if tier_name == "premium_optional" and enable_premium:
            enabled = True
        if enabled:
            yield tier_name, tier


def _scene_record(
    *,
    scene_id: str,
    references_used: Sequence[str],
    prompt_hash: str,
) -> dict[str, Any]:
    return {
        "scene_id": scene_id,
        "status": "pending",
        "review_status": "PENDING",
        "selected_tier": None,
        "provider": None,
        "model": None,
        "attempts": 0,
        "cost_usd": 0.0,
        "references_used": list(references_used),
        "output_path": None,
        "draft_path": None,
        "final_path": None,
        "error": None,
        "prompt_hash": prompt_hash,
    }


def _existing_records(output_dir: Path, resume: bool) -> dict[str, dict[str, Any]]:
    manifest_path = output_dir / "manifest.json"
    if not resume or not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        scene_id: record
        for scene_id, record in _manifest_scene_map(manifest).items()
        if record.get("status") == "generated" and record.get("draft_path")
    }


def _record_has_output(record: Mapping[str, Any]) -> bool:
    value = record.get("draft_path") or record.get("output_path")
    if not value:
        return False
    path = Path(str(value))
    if not path.is_absolute():
        path = ROOT / path
    return path.is_file()


def _write_summary(path: Path, manifest: Mapping[str, Any]) -> None:
    scenes = list(manifest["scenes"])
    statuses: dict[str, int] = {}
    for scene in scenes:
        statuses[scene["status"]] = statuses.get(scene["status"], 0) + 1
    lines = [
        "# Visual generation summary",
        "",
        f"- Canonical source: {manifest['canonical_source']}",
        "- Prompt compilation: in memory",
        f"- Visual profile: {manifest['visual_profile']}",
        f"- FIN lock: {manifest['fin_lock']}",
        f"- Total scenes requested: {len(scenes)}",
        f"- Total generated: {statuses.get('generated', 0)}",
        f"- Total failed: {statuses.get('failed', 0)}",
        f"- Total unknown billed timeout: {statuses.get(UNKNOWN_BILLED_TIMEOUT, 0)}",
        f"- Total skipped: {statuses.get('skipped_budget', 0)}",
        f"- Total dry run: {statuses.get('dry_run', 0)}",
        f"- Total resumed: {manifest['resumed_count']}",
        f"- Budget limit: ${manifest['budget']['limit_usd']:.4f}",
        f"- Cost this run: ${manifest['budget']['spent_usd']:.4f}",
        "",
        "## Scenes by tier",
        "",
        *[
            f"- {tier}: {sum(scene['selected_tier'] == tier for scene in scenes)}"
            for tier in (
                "cheap_draft",
                "mid_fallback",
                "premium_optional",
                DETERMINISTIC_RENDERER_ID,
            )
        ],
        "",
        "## Scene results",
        "",
        "| scene | status | review | tier | provider | attempts | cost | draft | final |",
        "|---|---|---|---|---|---:|---:|---|---|",
    ]
    for scene in scenes:
        lines.append(
            f"| {scene['scene_id']} | {scene['status']} | {scene['review_status']} | "
            f"{scene['selected_tier'] or '-'} | {scene['provider'] or '-'} | "
            f"{scene['attempts']} | ${scene['cost_usd']:.4f} | "
            f"{scene['draft_path'] or '-'} | {scene['final_path'] or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Next action",
            "",
            "Review PENDING scenes, then approve or mark UPGRADE_REQUESTED.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_review_index(path: Path, manifest: Mapping[str, Any]) -> None:
    cards: list[str] = []
    for scene in manifest["scenes"]:
        image_path = scene.get("final_path") or scene.get("draft_path")
        if image_path:
            rooted_image = _rooted_output_path(str(image_path))
            if rooted_image is not None:
                try:
                    image_source = rooted_image.relative_to(path.parent).as_posix()
                except ValueError:
                    image_source = str(image_path)
            else:
                image_source = str(image_path)
            image_markup = (
                f'<img src="{html.escape(image_source)}" '
                f'alt="{html.escape(str(scene["scene_id"]))}">'
            )
        else:
            image_markup = '<div class="empty">dry-run sem imagem</div>'
        cards.append(
            "<article class=\"card\">"
            f"{image_markup}"
            f"<h2>{html.escape(str(scene['scene_id']))}</h2>"
            f"<p>model: {html.escape(str(scene.get('model') or '-'))}</p>"
            f"<p>custo: ${float(scene.get('cost_usd') or 0):.4f}</p>"
            f"<p>review: <strong>{html.escape(str(scene.get('review_status') or 'PENDING'))}</strong></p>"
            "</article>"
        )
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Capital Oculto visual review</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f1ea;color:#20201d;margin:2rem}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}
.card{background:#fff;border:1px solid #d8d1c4;padding:1rem;border-radius:8px}
img,.empty{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#eee9df}
.empty{display:grid;place-items:center;color:#746f66;font-size:.9rem}
h1{margin-bottom:1.5rem}h2{margin:.8rem 0 .3rem;font-size:1.1rem}p{margin:.25rem 0;color:#5f5a52}
</style>
</head>
<body><h1>Capital Oculto — visual review</h1><main>
""" + "\n".join(cards) + """
</main></body></html>
"""
    path.write_text(document, encoding="utf-8")


def _write_review_outputs(output_dir: Path, manifest: Mapping[str, Any]) -> None:
    _write_json(output_dir / "manifest.json", manifest)
    _write_summary(output_dir / "summary.md", manifest)
    _write_review_index(output_dir / "review_index.html", manifest)


def _run_generation_unlocked(
    scene_ids: Sequence[str] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    visual_scenes_path: Path = DEFAULT_VISUAL_SCENES,
    output_dir: Path = DEFAULT_GENERATED_OUTPUT,
    budget_usd: float | None = None,
    enable_premium: bool = False,
    dry_run: bool | None = None,
    resume: bool | None = None,
    provider_registry: ProviderRegistry | None = None,
    run_id: str,
    request_reason: str,
    allowed_tiers: Sequence[str] | None = None,
    max_attempts_per_scene: int | None = None,
    carry_forward_costs: bool = False,
) -> dict[str, Any]:
    """Carrega cenas JSON, compila prompts em memória e executa o lote."""

    effective_config = dict(config or load_generation_config())
    effective_profile = dict(profile or load_reference_profile())
    scene_payload = load_visual_scenes(visual_scenes_path)
    setup_errors = validate_generation_config(effective_config)
    setup_errors.extend(validate_reference_profile(effective_profile, ROOT))
    setup_errors.extend(validate_visual_scenes(scene_payload))
    if scene_payload["character_lock"] != effective_profile["fin_lock"]:
        setup_errors.append("visual scenes character_lock does not match reference profile")
    if scene_payload["visual_profile"] != effective_profile["profile_id"]:
        setup_errors.append("visual scenes profile does not match reference profile")
    if setup_errors:
        raise ValueError("invalid visual pipeline setup: " + "; ".join(setup_errors))

    scenes = build_generation_jobs(scene_ids, visual_scenes_path=visual_scenes_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry = provider_registry or ProviderRegistry()
    effective_dry_run = bool(
        effective_config["dry_run"] if dry_run is None else dry_run
    )
    effective_resume = bool(
        effective_config.get("resume", True) if resume is None else resume
    )
    previous_manifest: dict[str, Any] | None = None
    previous_manifest_path = output_dir / "manifest.json"
    if previous_manifest_path.is_file():
        previous_manifest = json.loads(
            previous_manifest_path.read_text(encoding="utf-8")
        )
    budget = BudgetManager(
        effective_config["default_budget_usd"] if budget_usd is None else budget_usd
    )
    audit_log_path = output_dir / "request_audit.jsonl"
    request_sequence = _next_request_sequence(audit_log_path) - 1
    previous = _existing_records(output_dir, effective_resume and not effective_dry_run)
    all_previous_manifest_records = (
        _manifest_scene_map(previous_manifest) if previous_manifest else {}
    )
    previous_manifest_records = (
        all_previous_manifest_records if carry_forward_costs else {}
    )
    max_total_attempts = (
        1 + int(effective_config["max_retries_per_scene"])
        if max_attempts_per_scene is None
        else int(max_attempts_per_scene)
    )
    if max_total_attempts < 1:
        raise ValueError("max_attempts_per_scene must be at least 1")
    scene_results: list[dict[str, Any]] = []
    resumed_count = 0

    for scene in scenes:
        scene_id = str(scene["scene_id"])
        route = resolve_visual_route(scene, effective_config)
        route_tiers = routed_tier_names(
            route,
            effective_config,
            enable_premium=enable_premium,
            allowed_tiers=allowed_tiers,
        )
        references = (
            resolve_scene_references(scene, effective_profile)
            if route.api_required
            else []
        )
        compiled_prompt: str | None = None
        prompt_errors: list[str] = []
        if route.api_required:
            compiled_prompt = build_prompt(scene)
            prompt_errors = validate_compiled_prompt(compiled_prompt)
            hash_payload: Mapping[str, Any] = {
                "renderer": route.renderer,
                "prompt": compiled_prompt,
                "references": references,
                "tiers": [
                    {
                        "name": tier_name,
                        "provider": effective_config["tiers"][tier_name]["provider"],
                        "model": effective_config["tiers"][tier_name]["model"],
                    }
                    for tier_name in route_tiers
                ],
            }
        else:
            hash_payload = {
                "renderer": DETERMINISTIC_RENDERER_ID,
                "renderer_version": DETERMINISTIC_RENDERER_VERSION,
                "scene": scene,
            }
        hash_source = json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt_hash = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()
        record = _scene_record(
            scene_id=scene_id,
            references_used=references,
            prompt_hash=prompt_hash,
        )
        previous_scene_record = all_previous_manifest_records.get(scene_id)
        old_record = previous.get(scene_id) or previous_manifest_records.get(scene_id)
        previous_attempts = (
            int(old_record.get("attempts") or 0)
            if carry_forward_costs and old_record
            else 0
        )
        if (
            old_record
            and old_record.get("prompt_hash") == prompt_hash
            and _record_has_output(old_record)
        ):
            scene_results.append(dict(old_record))
            resumed_count += 1
            continue

        tiers = list(
            _enabled_tiers(
                effective_config,
                enable_premium=enable_premium,
                allowed_tiers=route_tiers,
            )
        )
        if prompt_errors:
            record["status"] = "failed"
            record["error"] = "; ".join(prompt_errors)
        elif route.renderer == DETERMINISTIC_LOCAL_RENDERER:
            record.update(
                {
                    "selected_tier": DETERMINISTIC_RENDERER_ID,
                    "provider": route.provider,
                    "model": route.model,
                }
            )
            if effective_dry_run:
                record["status"] = "dry_run"
            else:
                attempt = 1
                budget.charge(
                    scene_id=scene_id,
                    tier=DETERMINISTIC_RENDERER_ID,
                    attempt=attempt + previous_attempts,
                    amount_usd=route.cost_usd,
                )
                record["attempts"] = attempt
                record["cost_usd"] = route.cost_usd
                output_path = output_dir / "drafts" / f"{scene_id}.png"
                try:
                    rendered_path = render_deterministic_scene(scene, output_path)
                    image_validation = validate_generated_image(rendered_path)
                    if image_validation["passed"]:
                        generated_path = _display_path(rendered_path)
                        record["status"] = "generated"
                        record["output_path"] = generated_path
                        record["draft_path"] = generated_path
                    else:
                        record["status"] = "failed"
                        record["error"] = "; ".join(image_validation["errors"])
                except (OSError, ValueError) as exc:
                    record["status"] = "failed"
                    record["error"] = f"{type(exc).__name__}: {exc}"
                if (
                    record["status"] != "generated"
                    and previous_scene_record
                    and _record_has_output(previous_scene_record)
                ):
                    record["draft_path"] = previous_scene_record.get("draft_path")
        elif not tiers:
            record["status"] = "failed"
            record["error"] = "no generation tier is enabled for the scene route"
        elif effective_dry_run:
            tier_name, tier = tiers[0]
            record.update(
                {
                    "status": "dry_run",
                    "selected_tier": tier_name,
                    "provider": tier["provider"],
                    "model": tier["model"],
                }
            )
        else:
            last_error = "all enabled generation attempts failed"
            stop_for_budget = False
            stop_for_unknown_billed_timeout = False
            for tier_name, tier in tiers:
                if record["attempts"] >= max_total_attempts:
                    break
                for _ in range(int(tier["max_attempts"])):
                    if record["attempts"] >= max_total_attempts:
                        break
                    cost = float(tier["estimated_cost_per_image"])
                    record.update(
                        {
                            "selected_tier": tier_name,
                            "provider": tier["provider"],
                            "model": tier["model"],
                        }
                    )
                    if not budget.can_afford(cost):
                        stop_for_budget = True
                        last_error = (
                            f"insufficient budget: required ${cost:.4f}, "
                            f"remaining ${float(budget.remaining_usd):.4f}"
                        )
                        break
                    attempt = int(record["attempts"]) + 1
                    reservation = budget.charge(
                        scene_id=scene_id,
                        tier=tier_name,
                        attempt=attempt + previous_attempts,
                        amount_usd=cost,
                    )
                    record["attempts"] = attempt
                    output_path = output_dir / "drafts" / f"{scene_id}.png"
                    request_sequence += 1
                    request = GenerationRequest(
                        scene_id=scene_id,
                        prompt=str(compiled_prompt),
                        visual_profile=str(effective_profile["profile_id"]),
                        fin_lock=str(effective_profile["fin_lock"]),
                        reference_images=tuple(record["references_used"]),
                        provider=str(tier["provider"]),
                        model=str(tier["model"]),
                        tier=tier_name,
                        timeout=int(tier["timeout"]),
                        output_path=output_path,
                        request_sequence=request_sequence,
                        request_attempt=attempt + previous_attempts,
                        reason=request_reason,
                        audit_log_path=audit_log_path,
                        run_id=run_id,
                    )
                    if str(tier["provider"]) == "openrouter":
                        configure_api_key_environment()
                    started = perf_counter()
                    provider_result = registry.get(str(tier["provider"])).generate(request)
                    elapsed = perf_counter() - started
                    settled_cost = (
                        provider_result.cost_usd
                        if provider_result.cost_usd is not None
                        else cost
                    )
                    budget.settle(reservation, settled_cost)
                    record["cost_usd"] = round(
                        float(record["cost_usd"]) + float(settled_cost), 4
                    )
                    if provider_result.status == UNKNOWN_BILLED_TIMEOUT:
                        record["status"] = UNKNOWN_BILLED_TIMEOUT
                        record["review_status"] = "PENDING"
                        record["output_path"] = None
                        record["error"] = "TimeoutError"
                        if previous_scene_record and _record_has_output(previous_scene_record):
                            record["draft_path"] = previous_scene_record.get("draft_path")
                        stop_for_unknown_billed_timeout = True
                        break
                    if provider_result.success and provider_result.image_path:
                        image_validation = validate_generated_image(provider_result.image_path)
                        if image_validation["passed"]:
                            generated_path = _display_path(provider_result.image_path)
                            record["status"] = "generated"
                            record["output_path"] = generated_path
                            record["draft_path"] = generated_path
                            record["error"] = None
                            break
                        last_error = "; ".join(image_validation["errors"])
                    else:
                        last_error = provider_result.error or "provider generation failed"
                if (
                    record["status"] in {"generated", UNKNOWN_BILLED_TIMEOUT}
                    or stop_for_budget
                    or stop_for_unknown_billed_timeout
                ):
                    break
            if record["status"] not in {"generated", UNKNOWN_BILLED_TIMEOUT}:
                record["status"] = "skipped_budget" if stop_for_budget else "failed"
                record["error"] = last_error
        scene_results.append(record)

    if previous_manifest and not effective_dry_run:
        previous_records = _manifest_scene_map(previous_manifest)
        for record in scene_results:
            previous_record = previous_records.get(str(record["scene_id"]))
            if carry_forward_costs and previous_record:
                record["attempts"] = int(previous_record.get("attempts") or 0) + int(
                    record.get("attempts") or 0
                )
                record["cost_usd"] = round(
                    float(previous_record.get("cost_usd") or 0)
                    + float(record.get("cost_usd") or 0),
                    4,
                )
                if not record.get("final_path") and previous_record.get("final_path"):
                    record["final_path"] = previous_record["final_path"]
                if not record.get("draft_path") and previous_record.get("draft_path"):
                    record["draft_path"] = previous_record["draft_path"]
            previous_records[str(record["scene_id"])] = record
        previous_order = [
            str(record["scene_id"])
            for record in previous_manifest.get("scenes", [])
            if record.get("scene_id") in previous_records
        ]
        current_order = [str(record["scene_id"]) for record in scene_results]
        merged_order = previous_order + [
            scene_id for scene_id in current_order if scene_id not in previous_order
        ]
        scene_results = [previous_records[scene_id] for scene_id in merged_order]

    manifest: dict[str, Any] = {
        "schema_version": "1.1",
        "episode_id": scene_payload["episode_id"],
        "run_id": run_id,
        "reason": request_reason,
        "generated_at": _utc_now(),
        "canonical_source": _display_path(visual_scenes_path),
        "visual_profile": effective_profile["profile_id"],
        "fin_lock": effective_profile["fin_lock"],
        "dry_run": effective_dry_run,
        "premium_enabled": enable_premium
        or bool(effective_config["tiers"]["premium_optional"]["enabled"]),
        "resume_enabled": effective_resume,
        "resumed_count": resumed_count,
        "completed": not effective_dry_run,
        "request_audit_path": (
            _display_path(audit_log_path)
            if not effective_dry_run and audit_log_path.is_file()
            else None
        ),
        "budget": budget.snapshot(),
        "scenes": scene_results,
    }
    if previous_manifest and carry_forward_costs and not effective_dry_run:
        previous_budget = previous_manifest.get("budget", {})
        current_budget = manifest["budget"]
        entries = list(previous_budget.get("entries", [])) + list(
            current_budget.get("entries", [])
        )
        limit_usd = float(previous_budget.get("limit_usd") or current_budget["limit_usd"])
        spent_usd = round(
            sum(float(entry.get("amount_usd") or 0) for entry in entries),
            4,
        )
        manifest["budget"] = {
            "limit_usd": limit_usd,
            "spent_usd": spent_usd,
            "remaining_usd": round(limit_usd - spent_usd, 4),
            "entries": entries,
        }
    _write_review_outputs(output_dir, manifest)
    return manifest


def run_generation(
    scene_ids: Sequence[str] | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    visual_scenes_path: Path = DEFAULT_VISUAL_SCENES,
    output_dir: Path = DEFAULT_GENERATED_OUTPUT,
    budget_usd: float | None = None,
    enable_premium: bool = False,
    dry_run: bool | None = None,
    resume: bool | None = None,
    provider_registry: ProviderRegistry | None = None,
    allow_rerun: bool = False,
    rerun_reason: str | None = None,
    allowed_tiers: Sequence[str] | None = None,
    max_attempts_per_scene: int | None = None,
    carry_forward_costs: bool = False,
) -> dict[str, Any]:
    effective_config = dict(config or load_generation_config())
    effective_dry_run = bool(effective_config.get("dry_run", False) if dry_run is None else dry_run)
    effective_resume = bool(effective_config.get("resume", True) if resume is None else resume)
    run_id = uuid4().hex
    with _generation_lock(output_dir, dry_run=effective_dry_run, run_id=run_id):
        manifest_path = output_dir / "manifest.json"
        if manifest_path.is_file():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("completed") is True and not effective_dry_run:
                if not allow_rerun:
                    raise RuntimeError(
                        "a completed visual run already exists; rerun requires explicit authorization"
                    )
                if not rerun_reason or not rerun_reason.strip():
                    raise ValueError("an explicit rerun reason is required")
            if previous.get("completed") is True and effective_dry_run:
                raise RuntimeError(
                    "dry-run cannot overwrite a completed real generation; use a different --output"
                )
        request_reason = (
            f"generation_rerun:{rerun_reason.strip()}"
            if allow_rerun and rerun_reason
            else "generation"
        )
        return _run_generation_unlocked(
            scene_ids,
            config=effective_config,
            profile=profile,
            visual_scenes_path=visual_scenes_path,
            output_dir=output_dir,
            budget_usd=budget_usd,
            enable_premium=enable_premium,
            dry_run=effective_dry_run,
            resume=effective_resume,
            provider_registry=provider_registry,
            run_id=run_id,
            request_reason=request_reason,
            allowed_tiers=allowed_tiers,
            max_attempts_per_scene=max_attempts_per_scene,
            carry_forward_costs=carry_forward_costs,
        )


def _load_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"visual manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_episode_id = str(load_visual_scenes()["episode_id"])
    declared_episode_id = manifest.get("episode_id")
    if declared_episode_id is None:
        manifest["episode_id"] = expected_episode_id
    elif str(declared_episode_id) != expected_episode_id:
        raise ValueError(
            f"{EPISODE_ID_MISMATCH}: visual manifest declares "
            f"{declared_episode_id!r}; active episode is {expected_episode_id!r}"
        )
    return manifest


def _manifest_scene_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for scene in manifest.get("scenes", []):
        if not isinstance(scene, Mapping) or not scene.get("scene_id"):
            continue
        record = scene if isinstance(scene, dict) else dict(scene)
        if "output_path" not in record:
            record["output_path"] = (
                record.get("draft_path") if record.get("status") == "generated" else None
            )
        records[str(record["scene_id"])] = record
    return records


def _rooted_output_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def approve_scenes(
    scene_ids: Sequence[str],
    *,
    output_dir: Path = DEFAULT_GENERATED_OUTPUT,
) -> dict[str, Any]:
    run_id = uuid4().hex
    with _generation_lock(output_dir, dry_run=False, run_id=run_id):
        manifest = _load_manifest(output_dir)
        scenes = _manifest_scene_map(manifest)
        for scene_id in scene_ids:
            record = scenes.get(str(scene_id))
            if record is None:
                raise ValueError(f"scene is not present in manifest: {scene_id}")
            if record.get("status") not in {"generated", UNKNOWN_BILLED_TIMEOUT}:
                raise ValueError(f"scene is not generated and cannot be approved: {scene_id}")
            draft_path = _rooted_output_path(record.get("draft_path"))
            if draft_path is None or not draft_path.is_file():
                raise ValueError(f"draft image is missing for approval: {scene_id}")
            record["review_status"] = "APPROVED"
            if not record.get("final_path"):
                record["final_path"] = record["draft_path"]
        manifest["review_updated_at"] = _utc_now()
        _write_review_outputs(output_dir, manifest)
        return manifest


def _seed_budget_from_manifest(
    budget: BudgetManager,
    manifest: Mapping[str, Any],
) -> None:
    for record in manifest.get("scenes", []):
        amount = float(record.get("cost_usd") or 0)
        if amount <= 0:
            continue
        budget.seed(
            scene_id=str(record["scene_id"]),
            tier=str(record.get("selected_tier") or "unknown"),
            attempt=int(record.get("attempts") or 0),
            amount_usd=amount,
        )


def _next_request_sequence(audit_log_path: Path) -> int:
    if not audit_log_path.is_file():
        return 1
    maximum = 0
    for raw_line in audit_log_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            value = int(json.loads(raw_line).get("request_sequence") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        maximum = max(maximum, value)
    return maximum + 1


def upgrade_scenes(
    scene_ids: Sequence[str],
    *,
    output_dir: Path = DEFAULT_GENERATED_OUTPUT,
    budget_usd: float | None = None,
    config: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> dict[str, Any]:
    config_data = dict(config or load_generation_config())
    profile_data = dict(profile or load_reference_profile())
    mid_tier = config_data["tiers"]["mid_fallback"]
    if not bool(mid_tier["enabled"]):
        raise ValueError("mid_fallback tier is disabled")
    if str(mid_tier["model"]) != "openai/gpt-image-2":
        raise ValueError("mid_fallback must use openai/gpt-image-2")
    if int(mid_tier["max_attempts"]) != 1:
        raise ValueError("upgrade must allow exactly one GPT attempt")

    run_id = uuid4().hex
    with _generation_lock(output_dir, dry_run=False, run_id=run_id):
        manifest = _load_manifest(output_dir)
        scenes = _manifest_scene_map(manifest)
        selected = [scenes.get(str(scene_id)) for scene_id in scene_ids]
        if any(record is None for record in selected):
            missing = [
                str(scene_id)
                for scene_id, record in zip(scene_ids, selected)
                if record is None
            ]
            raise ValueError("scene is not present in manifest: " + ", ".join(missing))
        for record in selected:
            if record["review_status"] != "UPGRADE_REQUESTED":
                raise ValueError(
                    f"scene {record['scene_id']} must be UPGRADE_REQUESTED before upgrade"
                )

        budget = BudgetManager(
            config_data["default_budget_usd"] if budget_usd is None else budget_usd
        )
        _seed_budget_from_manifest(budget, manifest)
        output_dir.mkdir(parents=True, exist_ok=True)
        final_dir = output_dir / "final"
        audit_log_path = output_dir / "request_audit.jsonl"
        request_sequence = _next_request_sequence(audit_log_path)
        registry = provider_registry or ProviderRegistry()
        configure_api_key_environment()

        for record in selected:
            scene_id = str(record["scene_id"])
            scene = build_generation_jobs([scene_id])[0]
            prompt = build_prompt(scene)
            references = resolve_scene_references(scene, profile_data)
            record["references_used"] = references
            estimate = float(mid_tier["estimated_cost_per_image"])
            if not budget.can_afford(estimate):
                record["status"] = "skipped_budget"
                record["error"] = (
                    f"insufficient budget: required ${estimate:.4f}, "
                    f"remaining ${float(budget.remaining_usd):.4f}"
                )
                continue
            final_path = final_dir / f"{scene_id}.png"
            if final_path.exists():
                raise RuntimeError(f"upgrade output already exists: {final_path}")
            attempt = int(record["attempts"]) + 1
            reservation = budget.charge(
                scene_id=scene_id,
                tier="mid_fallback",
                attempt=attempt,
                amount_usd=estimate,
            )
            request = GenerationRequest(
                scene_id=scene_id,
                prompt=prompt,
                visual_profile=str(profile_data["profile_id"]),
                fin_lock=str(profile_data["fin_lock"]),
                reference_images=tuple(references),
                provider=str(mid_tier["provider"]),
                model=str(mid_tier["model"]),
                tier="mid_fallback",
                timeout=int(mid_tier["timeout"]),
                output_path=final_path,
                request_sequence=request_sequence,
                request_attempt=attempt,
                reason="visual_upgrade",
                audit_log_path=audit_log_path,
                run_id=run_id,
            )
            request_sequence += 1
            provider_result = registry.get(str(mid_tier["provider"])).generate(request)
            settled_cost = (
                provider_result.cost_usd
                if provider_result.cost_usd is not None
                else estimate
            )
            budget.settle(reservation, settled_cost)
            record["attempts"] = attempt
            record["selected_tier"] = "mid_fallback"
            record["provider"] = mid_tier["provider"]
            record["model"] = mid_tier["model"]
            record["cost_usd"] = round(
                float(record["cost_usd"]) + float(settled_cost), 4
            )
            if provider_result.success and provider_result.image_path:
                image_validation = validate_generated_image(provider_result.image_path)
                if image_validation["passed"]:
                    generated_path = _display_path(provider_result.image_path)
                    record["status"] = "generated"
                    record["review_status"] = "PENDING"
                    record["output_path"] = generated_path
                    record["final_path"] = generated_path
                    record["error"] = None
                    continue
                error = "; ".join(image_validation["errors"])
            else:
                error = provider_result.error or "provider generation failed"
            if provider_result.status == UNKNOWN_BILLED_TIMEOUT:
                record["status"] = UNKNOWN_BILLED_TIMEOUT
                record["review_status"] = "PENDING"
                record["output_path"] = None
                record["final_path"] = None
                record["error"] = "TimeoutError"
                continue
            record["status"] = "failed"
            record["review_status"] = "UPGRADE_REQUESTED"
            record["output_path"] = None
            record["final_path"] = None
            record["error"] = error

        manifest["run_id"] = run_id
        manifest["reason"] = "visual_upgrade"
        manifest["budget"] = budget.snapshot()
        manifest["request_audit_path"] = _display_path(audit_log_path)
        manifest["review_updated_at"] = _utc_now()
        _write_review_outputs(output_dir, manifest)
        return manifest


def _parse_scene_ids(value: str) -> tuple[str, ...]:
    scene_ids = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    if not scene_ids:
        raise argparse.ArgumentTypeError("--scenes requires at least one scene id")
    return scene_ids


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline de geração visual", add_help=add_help)
    commands = parser.add_subparsers(dest="visual_command", required=True)
    episode = resolve_active_episode(ROOT)
    scene_map = commands.add_parser(
        "scene-map", help="Reconstrói a timeline editorial usando WordBoundary oficial."
    )
    scene_map.add_argument("--visual-script", type=Path, default=episode.file("roteiro_visual.csv"))
    scene_map.add_argument("--visual-scenes", type=Path, default=episode.file("visual_scenes.json"))
    scene_map.add_argument("--timing", type=Path, default=ROOT / "output" / "audio" / episode.episode_id / "timing.json")
    scene_map.add_argument("--narration", type=Path, default=episode.file("roteiro_narracao.md"))
    scene_map.add_argument("--output", type=Path, default=episode.file("scene_map.json"))
    preview = commands.add_parser(
        "preview", help="Compila prompts e calcula rotas/custo sem chamar providers."
    )
    preview.add_argument("--output", type=Path, default=DEFAULT_GENERATED_OUTPUT)
    generate = commands.add_parser("generate", help="Compila e gera cenas do JSON canônico.")
    selection = generate.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scenes", type=_parse_scene_ids)
    selection.add_argument("--all", dest="all_scenes", action="store_true")
    generate.add_argument("--budget", type=float)
    generate.add_argument("--enable-premium", action="store_true")
    generate.add_argument("--dry-run", action="store_true", default=None)
    resume_group = generate.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true")
    resume_group.add_argument("--no-resume", dest="resume", action="store_false")
    generate.set_defaults(resume=None)
    generate.add_argument("--output", type=Path, default=DEFAULT_GENERATED_OUTPUT)
    generate.add_argument("--allow-rerun", action="store_true")
    generate.add_argument("--reason")
    generate.add_argument(
        "--only-tier",
        choices=("cheap_draft", "mid_fallback", "premium_optional"),
        help="Restringe a geração a um único tier, sem fallback.",
    )
    generate.add_argument(
        "--max-attempts-per-scene",
        type=int,
        help="Limita as tentativas novas de cada cena nesta execução.",
    )
    generate.add_argument(
        "--carry-forward-costs",
        action="store_true",
        help="Preserva custos e tentativas anteriores no manifest.",
    )

    approve = commands.add_parser(
        "approve", help="Aprova drafts sem fazer chamada de provider."
    )
    approve.add_argument("--scenes", required=True, type=_parse_scene_ids)
    approve.add_argument("--output", type=Path, default=DEFAULT_GENERATED_OUTPUT)

    upgrade = commands.add_parser(
        "upgrade", help="Gera somente o upgrade GPT de cenas marcadas para revisão."
    )
    upgrade.add_argument("--scenes", required=True, type=_parse_scene_ids)
    upgrade.add_argument("--budget", type=float)
    upgrade.add_argument("--output", type=Path, default=DEFAULT_GENERATED_OUTPUT)

    validate = commands.add_parser("validate", help="Valida JSON, referências e output.")
    validate.add_argument("--output", type=Path, default=DEFAULT_GENERATED_OUTPUT)

    benchmark = commands.add_parser(
        "benchmark", help="Compara modelos OpenRouter sem fallback entre candidatos."
    )
    benchmark.add_argument("--scene", required=True)
    benchmark.add_argument("--dry-run", action="store_true")
    benchmark.add_argument("--output", type=Path, default=DEFAULT_BENCHMARK_OUTPUT)
    benchmark.add_argument("--allow-rerun", action="store_true")
    benchmark.add_argument("--reason")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.visual_command == "scene-map":
        try:
            payload = build_scene_map(
                visual_script_path=args.visual_script,
                visual_scenes_path=args.visual_scenes,
                timing_path=args.timing,
                narration_path=args.narration,
                existing_scene_map_path=args.output,
            )
            write_scene_map(args.output, payload)
        except (SceneMapError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"FAIL: {exc}")
            return 2
        print(f"SCENE_MAP={_display_path(args.output)}")
        print(f"EDITORIAL_SCENES={payload['editorial_scene_count']}")
        print(f"VISUAL_SCENES_EXISTING={payload['visual_scenes_existing']}")
        print(f"VISUAL_SCENES_MISSING={payload['visual_scenes_missing']}")
        return 0
    if args.visual_command == "preview":
        episode = resolve_active_episode(ROOT)
        scene_map_payload = json.loads(episode.file("scene_map.json").read_text(encoding="utf-8-sig"))
        baseline_missing = scene_map_payload.get("missing_visual_scene_ids", [])
        preview_payload = preview_visual_specs(
            visual_scenes_path=episode.file("visual_scenes.json"),
            visual_script_path=episode.file("roteiro_visual.csv"),
            scene_map_path=episode.file("scene_map.json"), output_dir=args.output,
            cost_scene_ids=baseline_missing,
        )
        print(json.dumps(preview_payload, ensure_ascii=False, indent=2))
        return 0
    if args.visual_command == "benchmark":
        manifest = run_benchmark(
            scene_id=args.scene.upper(),
            dry_run=args.dry_run,
            output_dir=args.output,
            allow_rerun=args.allow_rerun,
            rerun_reason=args.reason,
        )
        print(f"scene: {manifest['scene_id']}")
        print(
            f"aspect_ratio: {manifest['aspect_ratio']}; resolution: {manifest['resolution']}; "
            f"n: {manifest['n']}; budget_cap: ${manifest['max_benchmark_cost_usd']:.2f}"
        )
        for result in manifest["results"]:
            print(
                f"model: {result['model']} | status: {result['status']} | "
                f"references: {','.join(result['references_used'])} | "
                f"output: {result['output_path']}"
            )
        print(f"benchmark: {_display_path(args.output / 'benchmark.json')}")
        return 0
    if args.visual_command == "validate":
        config = load_generation_config()
        profile = load_reference_profile()
        payload = load_visual_scenes()
        errors = validate_generation_config(config)
        errors.extend(validate_reference_profile(profile, ROOT))
        errors.extend(validate_visual_scenes(payload))
        errors.extend(validate_output(args.output, ROOT))
        if errors:
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print("PASS: canonical visual JSON, references and generated output are valid")
        return 0
    if args.visual_command == "approve":
        manifest = approve_scenes(args.scenes, output_dir=args.output)
        print(f"approved: {len(args.scenes)}")
        print(f"review_index: {_display_path(args.output / 'review_index.html')}")
        return 0
    if args.visual_command == "upgrade":
        manifest = upgrade_scenes(
            args.scenes,
            output_dir=args.output,
            budget_usd=args.budget,
        )
        print(f"upgraded: {len(args.scenes)}")
        print(f"manifest: {_display_path(args.output / 'manifest.json')}")
        print(
            f"spent: ${manifest['budget']['spent_usd']:.4f}; "
            f"remaining: ${manifest['budget']['remaining_usd']:.4f}"
        )
        return 0

    scene_ids = None if args.all_scenes else args.scenes
    manifest = run_generation(
        scene_ids,
        output_dir=args.output,
        budget_usd=args.budget,
        enable_premium=args.enable_premium,
        dry_run=args.dry_run,
        resume=args.resume,
        allow_rerun=args.allow_rerun,
        rerun_reason=args.reason,
        allowed_tiers=(args.only_tier,) if args.only_tier else None,
        max_attempts_per_scene=args.max_attempts_per_scene,
        carry_forward_costs=args.carry_forward_costs,
    )
    print(f"manifest: {_display_path(args.output / 'manifest.json')}")
    print(f"summary: {_display_path(args.output / 'summary.md')}")
    print(
        f"scenes: {len(manifest['scenes'])}; "
        f"spent: ${manifest['budget']['spent_usd']:.4f}; "
        f"remaining: ${manifest['budget']['remaining_usd']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

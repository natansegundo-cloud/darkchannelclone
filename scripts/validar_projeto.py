#!/usr/bin/env python3
"""Validação curta da árvore ativa do Capital Oculto."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.episodes import (  # noqa: E402
    load_json as load_path_json,
    load_visual_script,
    resolve_active_episode,
    validate_scene_map,
    validate_visual_script,
)
from src.narration.providers.pacing import BEATS_DATA, BEATS_METADATA  # noqa: E402
from src.narration.validation import (  # noqa: E402
    validate_delivery_contract,
    validate_pilot_narration,
)
from src.visuals.engine import validate_visual_scenes  # noqa: E402


REQUIRED = (
    "AGENTS.md",
    "README.md",
    "main.py",
    "requirements.txt",
    ".env.example",
    "config/project.json",
    "config/narrators.json",
    "config/motion_contract.json",
    "config/character_fin.json",
    "config/visual_generation.json",
    "config/visual_benchmark.json",
    "config/visual_reference_profile.json",
    "config/dependencies.md",
    "assets/character_bible/fin_poses.png",
    "assets/character_bible/fin_turnaround.png",
    "assets/visual_references/S004.png",
    "assets/visual_references/S007.png",
    "assets/visual_references/S009.png",
    "src/episodes.py",
    "src/narration/engine.py",
    "src/narration/validation.py",
    "src/visuals/engine.py",
    "src/visuals/prompt_builder.py",
    "src/visuals/providers.py",
    "src/visuals/budget_manager.py",
    "src/visuals/benchmark.py",
    "src/visuals/generation_runner.py",
    "src/visuals/validators.py",
    "scripts/gerar_narracao.py",
)
BANNED_SUFFIX = re.compile(r"(?:_v2|_v3|_final|_novo|_refined|_candidate)(?:\.|$)", re.IGNORECASE)
def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def validate() -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"arquivo obrigatório ausente: {relative}")

    episode = resolve_active_episode(ROOT)
    for name in (
        "episodio.json",
        "roteiro_narracao.md",
        "roteiro_visual.csv",
        "scene_map.json",
        "visual_scenes.json",
    ):
        if not episode.file(name).is_file():
            errors.append(f"arquivo obrigatório ausente: {episode.file(name).relative_to(ROOT)}")

    project = dict(episode.project)
    narrators = load_json("config/narrators.json")
    motion = load_json("config/motion_contract.json")
    fin = load_json("config/character_fin.json")
    visual_generation = load_json("config/visual_generation.json")
    visual_benchmark = load_json("config/visual_benchmark.json")
    visual_profile = load_json("config/visual_reference_profile.json")
    if project.get("canonical_provider") != "azure_sdk":
        errors.append("canonical_provider deve ser azure_sdk")
    if project.get("timing_quality") != "WORD_BOUNDARY_REAL":
        errors.append("timing_quality deve ser WORD_BOUNDARY_REAL")
    if narrators["narrators"][0].get("provider") != "azure_sdk":
        errors.append("narrador padrão não usa azure_sdk")
    if motion["voice_pacing"].get("provider") != "azure_sdk":
        errors.append("contrato de pacing não usa azure_sdk")
    errors.extend(validate_delivery_contract(narrators, motion))
    errors.extend(
        validate_pilot_narration(
            BEATS_DATA,
            BEATS_METADATA,
            motion,
            production_stage=episode.production_stage,
        )
    )
    if fin.get("character_lock_version") != "FIN_V1":
        errors.append("character_lock_version deve ser FIN_V1")
    canonical_references = fin.get("canonical_references", [])
    if canonical_references != [
        "assets/character_bible/fin_turnaround.png",
        "assets/character_bible/fin_poses.png",
    ]:
        errors.append("referências canônicas do FIN estão incorretas")
    visual_traits = {
        "head",
        "face",
        "hair",
        "clothing",
        "tie",
        "proportions",
        "hands",
        "legs",
        "shoes",
        "palette",
        "outline",
        "illustration_style",
    }
    if not visual_traits.issubset(fin):
        errors.append("lock FIN_V1 não contém todos os traços visuais estáveis")

    if visual_generation.get("default_visual_profile") != "ILLUSTRATED_V1":
        errors.append("default_visual_profile deve ser ILLUSTRATED_V1")
    if float(visual_generation.get("default_budget_usd", -1)) != 0.15:
        errors.append("orçamento visual padrão deve ser US$ 0.15")
    if visual_generation.get("default_provider") != "openrouter":
        errors.append("provider visual padrão deve ser openrouter")
    if visual_generation.get("quality_first") is not True:
        errors.append("QUALITY_FIRST deve permanecer true")
    if visual_generation.get("auto_quality_upgrade") is not False:
        errors.append("upgrade automático por qualidade deve permanecer desabilitado")
    expected_priority = [
        "narrative_adherence",
        "FIN_V1_consistency",
        "visual_quality",
        "continuity",
        "cost",
    ]
    if visual_generation.get("decision_priority") != expected_priority:
        errors.append("ordem de prioridade visual incorreta")
    if visual_generation.get("max_retries_per_scene") != 1:
        errors.append("pipeline visual deve permitir no máximo um fallback")
    expected_fallback = {
        "first_tier": "cheap_draft",
        "second_tier": "mid_fallback",
        "third_tier": "premium_optional",
    }
    if visual_generation.get("fallback_policy") != expected_fallback:
        errors.append("fallback visual deve seguir cheap, mid e premium")
    tiers = visual_generation.get("tiers", {})
    tier_fields = {
        "enabled",
        "provider",
        "model",
        "max_attempts",
        "timeout",
        "estimated_cost_per_image",
    }
    for tier_name in ("cheap_draft", "mid_fallback", "premium_optional"):
        if not tier_fields.issubset(tiers.get(tier_name, {})):
            errors.append(f"tier visual incompleto: {tier_name}")
    expected_tiers = {
        "cheap_draft": (True, "openrouter", "black-forest-labs/flux.2-klein-4b", 120),
        "mid_fallback": (True, "openrouter", "openai/gpt-image-2", 180),
        "premium_optional": (False, "openrouter", "openai/gpt-image-2", 180),
    }
    for tier_name, (enabled, provider, model, timeout) in expected_tiers.items():
        tier = tiers.get(tier_name, {})
        if tier.get("enabled") is not enabled:
            errors.append(f"enabled incorreto em {tier_name}")
        if tier.get("provider") != provider or tier.get("model") != model:
            errors.append(f"provider/model incorreto em {tier_name}")
        if tier.get("max_attempts") != 1:
            errors.append(f"max_attempts incorreto em {tier_name}")
        if tier.get("timeout") != timeout:
            errors.append(f"timeout incorreto em {tier_name}: esperado {timeout}s")
    if tiers.get("premium_optional", {}).get("enabled") is not False:
        errors.append("premium_optional deve vir desabilitado por padrão")
    if visual_generation.get("resume") is not True:
        errors.append("resume visual deve vir habilitado por padrão")

    benchmark_models = [
        "sourceful/riverflow-v2.5-fast:free",
        "openai/gpt-image-2",
        "black-forest-labs/flux.2-klein-4b",
        "bytedance-seed/seedream-4.5",
    ]
    if visual_benchmark.get("provider") != "openrouter":
        errors.append("provider do benchmark deve ser openrouter")
    if visual_benchmark.get("scene_id") != "S004":
        errors.append("benchmark deve permanecer restrito a S004")
    if visual_benchmark.get("aspect_ratio") != "16:9":
        errors.append("benchmark deve usar aspect_ratio 16:9")
    if visual_benchmark.get("resolution") != "1K" or visual_benchmark.get("n") != 1:
        errors.append("benchmark deve gerar uma única imagem em resolução 1K")
    if visual_benchmark.get("max_attempts_per_model") != 1:
        errors.append("benchmark deve permitir exatamente uma tentativa por modelo")
    if float(visual_benchmark.get("max_benchmark_cost_usd", -1)) != 0.15:
        errors.append("teto do benchmark deve ser US$0.15")
    if [item.get("model") for item in visual_benchmark.get("candidates", [])] != benchmark_models:
        errors.append("modelos do benchmark estão incorretos")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8-sig")
    if "OPENROUTER_API_KEY=" not in env_example:
        errors.append("OPENROUTER_API_KEY ausente em .env.example")

    if visual_profile.get("profile_id") != "ILLUSTRATED_V1":
        errors.append("perfil visual aprovado deve ser ILLUSTRATED_V1")
    if visual_profile.get("fin_lock") != "FIN_V1":
        errors.append("perfil visual deve preservar FIN_V1")
    profile_references = list(visual_profile.get("required_references", []))
    profile_references.extend(visual_profile.get("scene_reference_images", {}).values())
    if any(not (ROOT / reference).is_file() for reference in profile_references):
        errors.append("perfil visual aponta para referência ausente")

    timing_dir = episode.file("timing")
    for path in sorted(timing_dir.glob("b*.json")):
        timing = json.loads(path.read_text(encoding="utf-8-sig"))
        if timing.get("provider") != "azure_speech_sdk":
            errors.append(f"provider inválido em {path.name}")
        if timing.get("timing_quality") != "WORD_BOUNDARY_REAL":
            errors.append(f"timing_quality inválido em {path.name}")
        if any(not beat.get("synthesis_id") for beat in timing.get("beats", [])):
            errors.append(f"synthesis_id ausente em {path.name}")

    visual_rows = load_visual_script(episode.file("roteiro_visual.csv"))
    errors.extend(validate_visual_script(visual_rows))
    planned_scene_ids = [str(row.get("scene_id", "")).strip() for row in visual_rows]
    planned_scene_types = {
        str(row.get("scene_id", "")).strip(): str(row.get("scene_type", "")).strip()
        for row in visual_rows
    }
    if any((row.get("text_on_screen") or "").strip() for row in visual_rows):
        errors.append("roteiro_visual não deve embutir texto nas imagens")

    scene_map = load_path_json(episode.file("scene_map.json"))
    errors.extend(
        validate_scene_map(
            scene_map,
            expected_episode_id=episode.episode_id,
            planned_scene_ids=planned_scene_ids,
            planned_scene_types=planned_scene_types,
            production_stage=episode.production_stage,
            expected_timing_quality=str(project.get("timing_quality", "")) or None,
        )
    )

    visual_scenes = load_path_json(episode.file("visual_scenes.json"))
    errors.extend(
        validate_visual_scenes(
            visual_scenes,
            expected_episode_id=episode.episode_id,
            planned_scene_ids=planned_scene_ids,
            planned_scene_types=planned_scene_types,
            production_stage=episode.production_stage,
        )
    )

    tracked = tracked_files()
    secrets = [path for path in tracked if path == ".env" or path.endswith("/.env")]
    if secrets:
        errors.append("segredo rastreado: " + ", ".join(secrets))
    banned = [path for path in tracked if BANNED_SUFFIX.search(Path(path).name)]
    if banned:
        errors.append("nome com sufixo proibido: " + ", ".join(banned))

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8-sig")
    for rule in (".env", "scripts/.env", "output/"):
        if rule not in gitignore:
            errors.append(f"regra ausente no .gitignore: {rule}")
    return errors


def main() -> int:
    try:
        errors = validate()
    except (OSError, KeyError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: projeto, narração e pipeline visual em tiers validados")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

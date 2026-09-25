#!/usr/bin/env python3
"""Validação curta da árvore ativa do Capital Oculto."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    "config/dependencies.md",
    "assets/character_bible/fin.svg",
    "episodios/CO-001/roteiro_narracao.md",
    "episodios/CO-001/roteiro_visual.csv",
    "episodios/CO-001/scene_map.json",
    "src/narration/engine.py",
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

    project = load_json("config/project.json")
    narrators = load_json("config/narrators.json")
    motion = load_json("config/motion_contract.json")
    if project.get("canonical_provider") != "azure_sdk":
        errors.append("canonical_provider deve ser azure_sdk")
    if project.get("timing_quality") != "WORD_BOUNDARY_REAL":
        errors.append("timing_quality deve ser WORD_BOUNDARY_REAL")
    if narrators["narrators"][0].get("provider") != "azure_sdk":
        errors.append("narrador padrão não usa azure_sdk")
    if motion["voice_pacing"].get("provider") != "azure_sdk":
        errors.append("contrato de pacing não usa azure_sdk")

    timing_dir = ROOT / "episodios" / "CO-001" / "timing"
    for path in sorted(timing_dir.glob("b*.json")):
        timing = json.loads(path.read_text(encoding="utf-8-sig"))
        if timing.get("provider") != "azure_speech_sdk":
            errors.append(f"provider inválido em {path.name}")
        if timing.get("timing_quality") != "WORD_BOUNDARY_REAL":
            errors.append(f"timing_quality inválido em {path.name}")
        if any(not beat.get("synthesis_id") for beat in timing.get("beats", [])):
            errors.append(f"synthesis_id ausente em {path.name}")

    scene_map = load_json("episodios/CO-001/scene_map.json")
    scenes = scene_map.get("scenes", [])
    if scene_map.get("timing_quality") != "WORD_BOUNDARY_REAL" or len(scenes) != 18:
        errors.append("scene_map deve conter S001-S018 com WORD_BOUNDARY_REAL")
    if any(float(scene["end"]) <= float(scene["start"]) for scene in scenes):
        errors.append("scene_map contém duração inválida")

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
    print("PASS: projeto enxuto, Azure SDK e WORD_BOUNDARY_REAL validados")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

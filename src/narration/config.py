"""Leitura centralizada da configuração de narração."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from src.episodes import resolve_active_episode


ROOT = Path(__file__).resolve().parents[2]
ACTIVE_EPISODE = resolve_active_episode(ROOT)
NARRATORS_PATH = ROOT / "config" / "narrators.json"
LOCAL_VOICE_PATH = ROOT / "config" / "voz_local.json"
MOTION_CONTRACT_PATH = ROOT / "config" / "motion_contract.json"
DEFAULT_ENV_PATH = ROOT / "scripts" / ".env"
DEFAULT_INPUT = ACTIVE_EPISODE.file("roteiro_narracao.md")
DEFAULT_OUTPUT = ROOT / "output" / "audio" / ACTIVE_EPISODE.episode_id


class ConfigError(RuntimeError):
    """Configuração ausente ou incompatível com o pipeline."""


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Configuração não encontrada: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def project_path(value: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    values = {key: value for key, value in os.environ.items() if value}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            if key in values:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value
    return values


def first_configured(values: dict[str, str], names: list[str]) -> str | None:
    return next((values[name].strip() for name in names if values.get(name, "").strip()), None)


def load_narrators() -> dict[str, Any]:
    return load_json(NARRATORS_PATH)


def narrator_by_id(config: dict[str, Any], narrator_id: str) -> dict[str, Any]:
    for narrator in config.get("narrators", []):
        if narrator.get("id") == narrator_id:
            return narrator
    raise ConfigError(f"Narrador não configurado: {narrator_id}")


def default_narrator() -> dict[str, Any]:
    config = load_narrators()
    return narrator_by_id(config, config["default_narrator"])


def azure_credentials(narrator: dict[str, Any], env_file: Path) -> tuple[str, str]:
    env = load_env(env_file)
    azure = narrator.get("azure", {})
    key = first_configured(env, azure.get("key_names", ["AZURE_SPEECH_KEY", "SPEECH_KEY"]))
    region = first_configured(env, azure.get("region_names", ["AZURE_SPEECH_REGION", "SPEECH_REGION"]))
    if not key or not region:
        raise ConfigError("Credenciais Azure ausentes em scripts/.env ou no ambiente.")
    return key, region


def load_local_voice() -> dict[str, Any]:
    return load_json(LOCAL_VOICE_PATH)


def load_motion_contract() -> dict[str, Any]:
    return load_json(MOTION_CONTRACT_PATH)

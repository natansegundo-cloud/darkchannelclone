"""Validacao pura do contrato de delivery e do material piloto."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


CANONICAL_VOICE = "pt-BR-HumbertoNeural"
CANONICAL_RATE = "-7%"
CANONICAL_PITCH = "0%"
CANONICAL_OUTPUT_FORMAT = "riff-48khz-16bit-mono-pcm"
PILOT_BEATS_SCOPE = "pilot_beats"


def _default_narrator(
    narrators: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    narrator_id = narrators.get("default_narrator")
    for narrator in narrators.get("narrators", []):
        if narrator.get("id") == narrator_id:
            return narrator
    return None


def validate_delivery_contract(
    narrators: Mapping[str, Any],
    motion_contract: Mapping[str, Any],
) -> list[str]:
    """Garante que duplicatas espelham a autoridade voice_pacing."""

    errors: list[str] = []
    narrator = _default_narrator(narrators)
    if narrator is None:
        return ["default narrator is not configured"]
    pacing = motion_contract.get("voice_pacing")
    if not isinstance(pacing, Mapping):
        return ["motion_contract.voice_pacing must be an object"]
    if pacing.get("voice") != CANONICAL_VOICE:
        errors.append(f"canonical voice must be {CANONICAL_VOICE}")
    if pacing.get("rate") != CANONICAL_RATE:
        errors.append(f"canonical rate must be {CANONICAL_RATE}")
    if pacing.get("pitch") != CANONICAL_PITCH:
        errors.append(f"canonical pitch must be {CANONICAL_PITCH}")
    if narrator.get("provider") != pacing.get("provider"):
        errors.append("narrator provider must match motion contract")
    if narrator.get("voice") != pacing.get("voice"):
        errors.append("narrator voice must match motion contract")
    azure = narrator.get("azure", {})
    for field, expected in (
        ("output_format", CANONICAL_OUTPUT_FORMAT), ("sample_rate", 48000),
        ("bit_depth", 16), ("channels", 1),
    ):
        if pacing.get(field) != expected:
            errors.append(f"canonical {field} must be {expected}")
        if not isinstance(azure, Mapping) or azure.get(field) != pacing.get(field):
            errors.append(f"narrator {field} must match motion contract")
    delivery = narrator.get("delivery", {})
    if not isinstance(delivery, Mapping):
        errors.append("narrator delivery must be an object")
    else:
        for field in ("rate", "pitch", "volume"):
            if field in delivery and delivery.get(field) != pacing.get(field):
                errors.append(f"narrator {field} must match motion contract")
    pauses = pacing.get("pauses_ms")
    if not isinstance(pauses, Mapping) or "beat_end" not in pauses:
        errors.append("motion contract must define beat_end pauses")
    density = pacing.get("speech_density")
    if not isinstance(density, Mapping) or not {
        "ideal_min_words_per_sec",
        "ideal_max_words_per_sec",
        "warning_threshold_words_per_sec",
    }.issubset(density):
        errors.append("motion contract must define speech density")
    return errors


def apply_operational_delivery(
    narrator: Mapping[str, Any],
    motion_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Aplica ao narrator a unica autoridade operacional de delivery."""

    pacing = motion_contract["voice_pacing"]
    resolved = deepcopy(dict(narrator))
    resolved["voice"] = pacing["voice"]
    delivery = resolved.setdefault("delivery", {})
    for field in ("rate", "pitch", "volume"):
        delivery[field] = pacing[field]
    return resolved


def validate_pilot_narration(
    beats: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    motion_contract: Mapping[str, Any],
    *,
    production_stage: str,
) -> list[str]:
    """Valida o piloto sem tratá-lo como cobertura universal do episodio."""

    errors: list[str] = []
    if metadata.get("scope") != PILOT_BEATS_SCOPE:
        errors.append("pilot narration scope must be pilot_beats")
    if metadata.get("coverage") == "full" or metadata.get("official_narration") is True:
        errors.append("pilot narration cannot claim full or official coverage")
    if production_stage not in {"visual_qualification", "production"}:
        errors.append("unsupported production_stage")
    beat_ids = [str(beat.get("beat_id", "")).strip() for beat in beats]
    if any(not beat_id for beat_id in beat_ids):
        errors.append("pilot beat_id must not be empty")
    if len(beat_ids) != len(set(beat_ids)):
        errors.append("pilot beat_ids must be unique")
    pacing = motion_contract.get("voice_pacing", {})
    beat_end_range = pacing.get("pauses_ms", {}).get("beat_end")
    if not isinstance(beat_end_range, list) or len(beat_end_range) != 2:
        errors.append("motion contract beat_end pause range is invalid")
    else:
        minimum, maximum = (int(value) for value in beat_end_range)
        for beat in beats:
            pause = beat.get("pause_after_ms")
            if not isinstance(pause, int) or not minimum <= pause <= maximum:
                errors.append(
                    f"{beat.get('beat_id', 'UNKNOWN')}: pilot pause violates motion contract"
                )
    return errors

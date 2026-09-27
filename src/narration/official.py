"""Official narration: strict per-beat cache and resumable Azure synthesis."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config
from .providers import ProviderError, ProviderResult
from .providers.pacing import read_pcm_wav, write_pcm_wav
from .providers.pacing import normalize_word
import re


class OfficialNarrationError(RuntimeError):
    pass


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_wav(path: Path, sample_rate: int, samples: array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".wav.tmp", dir=path.parent)
    os.close(fd)
    try:
        write_pcm_wav(Path(name), sample_rate, samples)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _levels(samples: array) -> tuple[float, float, int]:
    if not samples:
        raise OfficialNarrationError("Official audio is empty")
    peak = max(abs(int(value)) for value in samples)
    rms = math.sqrt(sum(int(value) ** 2 for value in samples) / len(samples))
    db = lambda value: -120.0 if value <= 0 else 20 * math.log10(value / 32768.0)
    clipping = sum(abs(int(value)) >= 32767 for value in samples)
    return round(db(peak), 4), round(db(rms), 4), clipping


def minimal_master(samples: array, sample_rate: int) -> tuple[array, dict[str, Any]]:
    """Preserve native dynamics; attenuate linearly only when headroom needs it."""
    raw_peak, raw_rms, raw_clipping = _levels(samples)
    target_peak = -3.0
    gain_db = min(0.0, target_peak - raw_peak)
    gain = 10 ** (gain_db / 20)
    processed = array("h", (
        max(-32768, min(32767, round(int(value) * gain))) for value in samples
    ))
    final_peak, final_rms, clipping = _levels(processed)
    if clipping:
        raise OfficialNarrationError("Clipping detected after minimal official mastering")
    return processed, {
        "implementation": "linear_headroom_only",
        "target_peak_dbfs": target_peak,
        "gain_db": round(gain_db, 4),
        "raw_peak_dbfs": raw_peak, "raw_rms_dbfs": raw_rms,
        "final_peak_dbfs": final_peak, "final_rms_dbfs": final_rms,
        "raw_clipping_samples": raw_clipping, "clipping_samples": clipping,
        "heavy_processing": False, "duration_preserved": True,
        "sample_rate": sample_rate,
    }


def _valid_boundaries(value: Any) -> bool:
    required = {"text", "normalized", "audio_offset_ms", "duration_ms", "boundary_type", "text_offset", "word_length"}
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and required <= item.keys() and item["audio_offset_ms"] is not None
        for item in value
    )


def _cached(
    raw_dir: Path,
    beat: dict[str, Any],
    identity: dict[str, Any],
    *,
    allow_legacy_synthesis_hash: bool = False,
) -> tuple[int, array, dict[str, Any]] | None:
    wav_path = raw_dir / f"{beat['beat_id']}.wav"
    meta_path = raw_dir / f"{beat['beat_id']}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected_values = {
            **identity,
            "beat_id": beat["beat_id"],
            "source_hash": beat["source_hash"],
            "synthesis_input_hash": beat["synthesis_input_hash"],
            "raw_text": beat["raw_text"],
        }
        legacy_hash_upgrade = False
        for key, expected in expected_values.items():
            if (
                key == "synthesis_input_hash"
                and allow_legacy_synthesis_hash
                and not beat.get("delivery_cues")
                and "synthesis_input_hash" not in meta
            ):
                legacy_hash_upgrade = True
                continue
            if meta.get(key) != expected:
                return None
        if (meta.get("completed") is not True
                or meta.get("timing_quality") != "WORD_BOUNDARY_REAL"
                or meta.get("same_synthesis_audio_timing") is not True):
            return None
        if not meta.get("synthesis_id") or not _valid_boundaries(meta.get("word_boundaries")):
            return None
        if not wav_path.is_file() or _sha_file(wav_path) != meta.get("audio_sha256"):
            return None
        rate, samples = read_pcm_wav(wav_path.read_bytes())
        if rate != meta.get("sample_rate") or not samples:
            return None
        if legacy_hash_upgrade:
            meta["synthesis_input_hash"] = beat["synthesis_input_hash"]
            meta["_legacy_synthesis_hash_upgrade"] = True
        return rate, samples, meta
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ProviderError):
        return None


def _benchmark_cached(
    root: Path | None, beat: dict[str, Any], identity: dict[str, Any]
) -> tuple[int, array, dict[str, Any], bytes] | None:
    if root is None or not root.is_dir():
        return None
    for run_dir in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        voice_dir = run_dir / "humberto"
        wav_path, meta_path = voice_dir / f"{beat['beat_id']}.wav", voice_dir / "metadata.json"
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            row = payload["beats"][beat["beat_id"]]
            if any((
                payload.get("episode_id") != identity["episode_id"],
                payload.get("voice") != identity["voice"],
                payload.get("rate") != identity["rate"],
                payload.get("pitch") != identity["pitch"],
                payload.get("format") != "RIFF 48KHZ 16BIT MONO PCM",
                row.get("voice") != identity["voice"],
                row.get("source_hash") != beat["source_hash"],
                row.get("benchmark_input_hash") != beat["synthesis_input_hash"],
                row.get("delivery_cues", []) != beat.get("delivery_cues", []),
                row.get("sample_rate") != identity["sample_rate"],
                row.get("bit_depth") != identity["bit_depth"],
                row.get("timing_quality") != "WORD_BOUNDARY_REAL",
                row.get("same_synthesis_audio_timing") is not True,
                not row.get("synthesis_id"),
                not _valid_boundaries(row.get("word_boundaries")),
                not wav_path.is_file(),
                _sha_file(wav_path) != row.get("audio_sha256"),
            )):
                continue
            audio_data = wav_path.read_bytes()
            rate, samples = read_pcm_wav(audio_data)
            if rate != identity["sample_rate"] or not samples:
                continue
            _validate_spoken_sequence(beat, row["word_boundaries"])
            meta = {
                **identity, "beat_id": beat["beat_id"],
                "source_hash": beat["source_hash"],
                "synthesis_input_hash": beat["synthesis_input_hash"],
                "raw_text": beat["raw_text"], "sample_rate": rate,
                "audio_sha256": row["audio_sha256"],
                "timing_quality": "WORD_BOUNDARY_REAL",
                "same_synthesis_audio_timing": True,
                "synthesis_id": row["synthesis_id"],
                "word_boundaries": row["word_boundaries"],
                "cache_source": f"azure_48k_benchmark/{run_dir.name}",
                "completed": True,
            }
            return rate, samples, meta, audio_data
        except (OSError, ValueError, KeyError, json.JSONDecodeError, ProviderError):
            continue
    return None


def preserve_official_rollback(
    *, output_path: Path, timing_path: Path, raw_dir: Path
) -> Path | None:
    """Copy the superseded Antonio official set once, without deleting it."""
    if not timing_path.is_file():
        return None
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    if timing.get("voice") != "pt-BR-AntonioNeural":
        return None
    rollback = output_path.parent / "rollback" / "antonio_24k"
    if rollback.exists():
        return rollback
    rollback.mkdir(parents=True)
    if output_path.is_file():
        shutil.copy2(output_path, rollback / output_path.name)
    shutil.copy2(timing_path, rollback / timing_path.name)
    if raw_dir.is_dir():
        shutil.copytree(raw_dir, rollback / "raw")
    _atomic_json(rollback / "rollback.json", {
        "voice": "pt-BR-AntonioNeural", "status": "SUPERSEDED",
        "source_audio_sha256": _sha_file(output_path) if output_path.is_file() else None,
        "preserved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    return rollback


def _words(boundaries: list[dict[str, Any]], offset: float, first_index: int) -> list[dict[str, Any]]:
    result = []
    for position, boundary in enumerate(boundaries, 1):
        start = offset + float(boundary["audio_offset_ms"]) / 1000
        duration = float(boundary.get("duration_ms") or 0) / 1000
        result.append({
            "index": first_index + position - 1, "beat_word_index": position,
            "text": boundary["text"], "normalized": boundary["normalized"],
            "start": round(start, 4), "end": round(start + duration, 4),
            **{key: boundary.get(key) for key in ("audio_offset_ms", "duration_ms", "boundary_type", "text_offset", "word_length")},
        })
    return result


def _validate_spoken_sequence(beat: dict[str, Any], boundaries: list[dict[str, Any]]) -> None:
    expected = [normalize_word(token) for token in re.findall(r"\S+", beat["raw_text"]) if normalize_word(token)]
    actual = [str(boundary.get("normalized") or normalize_word(str(boundary.get("text", "")))) for boundary in boundaries]
    # Azure may group adjacent spoken tokens in one WordBoundary event (for
    # example, "481 estudantes"). Validate the normalized spoken stream,
    # rather than assuming one event equals one whitespace-delimited token.
    if not actual or "".join(actual) != "".join(expected):
        raise OfficialNarrationError(
            f"{beat['beat_id']}: WordBoundary sequence differs from raw_text "
            f"(expected {len(expected)} words, received {len(actual)})"
        )


def run_official_narration(
    *, plan: dict[str, Any], narrator: dict[str, Any], contract: dict[str, Any],
    output_path: Path, timing_path: Path, raw_dir: Path, raw_combined_path: Path,
    synthesize: Callable[..., ProviderResult], key: str, region: str,
    processor: Callable[[array, int], tuple[array, dict[str, Any]]] = minimal_master,
    benchmark_cache_root: Path | None = None,
) -> dict[str, Any]:
    identity = {
        "episode_id": plan["episode_id"], "provider": "azure_sdk",
        "voice": plan["voice"], "rate": plan["rate"], "pitch": plan["pitch"],
        "output_format": plan.get("output_format", "riff-48khz-16bit-mono-pcm"),
        "sample_rate": int(plan.get("sample_rate", 48000)),
        "bit_depth": int(plan.get("bit_depth", 16)),
        "channels": int(plan.get("channels", 1)),
    }
    if identity["sample_rate"] != 48000 or identity["bit_depth"] != 16 or identity["channels"] != 1:
        raise OfficialNarrationError("Official format must be native 48 kHz 16-bit mono PCM")
    acquired: list[tuple[dict[str, Any], int, array, dict[str, Any]]] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".official-staging-", dir=output_path.parent))
    staging_raw_dir = staging_root / "raw"
    staged_cache_files: list[tuple[Path, Path, Path, Path]] = []
    staged_metadata_upgrades: list[tuple[Path, Path]] = []
    benchmark_reused: list[str] = []
    official_reused: list[str] = []
    azure_calls = 0

    def commit_staged_caches() -> None:
        for staged_wav, staged_json, target_wav, target_json in staged_cache_files:
            os.replace(staged_wav, target_wav)
            os.replace(staged_json, target_json)
        staged_cache_files.clear()

    try:
        for beat in plan["beats"]:
            cached = _cached(
                raw_dir,
                beat,
                identity,
                allow_legacy_synthesis_hash=True,
            )
            if cached is None:
                benchmark = _benchmark_cached(benchmark_cache_root, beat, identity)
                if benchmark is not None:
                    rate, samples, meta, audio_data = benchmark
                    wav_path = staging_raw_dir / f"{beat['beat_id']}.wav"
                    _atomic_bytes(wav_path, audio_data)
                    metadata_path = staging_raw_dir / f"{beat['beat_id']}.json"
                    _atomic_json(metadata_path, meta)
                    staged_cache_files.append((wav_path, metadata_path, raw_dir / f"{beat['beat_id']}.wav", raw_dir / f"{beat['beat_id']}.json"))
                    cached = (rate, samples, meta)
                    benchmark_reused.append(beat["beat_id"])
                else:
                    result = synthesize(
                        beat, narrator=narrator, key=key, region=region,
                        output_sample_rate=identity["sample_rate"],
                    )
                    azure_calls += 1
                    rate, samples = read_pcm_wav(result.audio_data)
                    if rate != identity["sample_rate"]:
                        raise OfficialNarrationError(f"{beat['beat_id']}: Azure returned {rate} Hz instead of 48000 Hz")
                    if result.metadata.get("timing_quality") != "WORD_BOUNDARY_REAL" or not result.metadata.get("synthesis_id") or not _valid_boundaries(result.boundaries):
                        raise OfficialNarrationError(f"Invalid real WordBoundary result for {beat['beat_id']}")
                    _validate_spoken_sequence(beat, result.boundaries)
                    wav_path = staging_raw_dir / f"{beat['beat_id']}.wav"
                    _atomic_bytes(wav_path, result.audio_data)
                    meta = {**identity, "beat_id": beat["beat_id"], "source_hash": beat["source_hash"], "synthesis_input_hash": beat["synthesis_input_hash"], "raw_text": beat["raw_text"],
                            "sample_rate": rate, "audio_sha256": _sha_file(wav_path), "timing_quality": "WORD_BOUNDARY_REAL",
                            "same_synthesis_audio_timing": True, "synthesis_id": result.metadata["synthesis_id"], "word_boundaries": result.boundaries, "cache_source": "azure_live", "completed": True}
                    metadata_path = staging_raw_dir / f"{beat['beat_id']}.json"
                    _atomic_json(metadata_path, meta)
                    staged_cache_files.append((wav_path, metadata_path, raw_dir / f"{beat['beat_id']}.wav", raw_dir / f"{beat['beat_id']}.json"))
                    cached = (rate, samples, meta)
            elif cached[2].pop("_legacy_synthesis_hash_upgrade", False):
                upgrade_path = staging_root / "metadata" / f"{beat['beat_id']}.json"
                _atomic_json(upgrade_path, cached[2])
                staged_metadata_upgrades.append(
                    (upgrade_path, raw_dir / f"{beat['beat_id']}.json")
                )
            else:
                official_reused.append(beat["beat_id"])
            acquired.append((beat, *cached))
    except Exception as exc:
        # Preserve resumability for beats completed before a later failure,
        # while leaving the failed beat and the official aggregate untouched.
        commit_staged_caches()
        shutil.rmtree(staging_root, ignore_errors=True)
        if isinstance(exc, OfficialNarrationError):
            raise
        raise OfficialNarrationError(f"Official synthesis failed at {beat['beat_id']}: {exc}") from exc

    rates = {item[1] for item in acquired}
    if len(rates) != 1:
        raise OfficialNarrationError("Sample-rate mismatch between official beats")
    sample_rate = rates.pop()
    if sample_rate != identity["sample_rate"]:
        raise OfficialNarrationError("Official aggregate is not native 48 kHz")
    pause_range = contract["voice_pacing"]["pauses_ms"]["beat_end"]
    pause_ms = round((int(pause_range[0]) + int(pause_range[1])) / 2)
    combined, beats_out, pauses, warnings = array("h"), [], [], []
    word_index = 1
    threshold = float(contract["voice_pacing"]["speech_density"]["warning_threshold_words_per_sec"])
    for index, (beat, _rate, samples, meta) in enumerate(acquired):
        offset = len(combined) / sample_rate
        _validate_spoken_sequence(beat, meta["word_boundaries"])
        words = _words(meta["word_boundaries"], offset, word_index)
        word_index += len(words)
        speech_start, speech_end = words[0]["start"], words[-1]["end"]
        density = round(len(words) / max(.1, speech_end - speech_start), 2)
        if density > threshold:
            warnings.append(f"VOICE_DENSITY_WARNING em {beat['beat_id']}: {density} palavras/s (> {threshold})")
        combined.extend(samples)
        beats_out.append({"beat_id": beat["beat_id"], "title": beat["title"], "text": beat["raw_text"], "source_hash": beat["source_hash"], "synthesis_input_hash": beat["synthesis_input_hash"],
                          "start": round(offset, 4), "speech_start": speech_start, "speech_end": speech_end,
                          "end": round(len(combined) / sample_rate, 4), "words_count": len(words), "words_per_second": density,
                          "raw_audio_file": config.relative_path(raw_dir / f"{beat['beat_id']}.wav"), "raw_audio_sha256": meta["audio_sha256"],
                          "synthesis_id": meta["synthesis_id"], "words": words})
        if index < len(acquired) - 1:
            start = len(combined) / sample_rate
            combined.extend(array("h", [0]) * round(sample_rate * pause_ms / 1000))
            pauses.append({"after_beat_id": beat["beat_id"], "start": round(start, 4), "end": round(len(combined) / sample_rate, 4),
                           "duration_ms": pause_ms, "source": "motion_contract.voice_pacing.pauses_ms.beat_end"})
    staged_raw_combined_path = staging_root / "narration_raw.wav"
    _atomic_wav(staged_raw_combined_path, sample_rate, combined)
    processed, processing = processor(combined, sample_rate)
    if len(processed) != len(combined) or processing.get("duration_preserved") is not True:
        raise OfficialNarrationError("Audio processing changed official narration duration")
    overlap = sum(a["speech_end"] > b["speech_start"] for a, b in zip(beats_out, beats_out[1:]))
    if overlap:
        raise OfficialNarrationError("Speech overlap detected in official narration")
    staged_output_path = staging_root / "narration.wav"
    _atomic_wav(staged_output_path, sample_rate, processed)
    timing = {"schema_version": "3.0", "episode_id": plan["episode_id"], "scope": "official_narration", "official_narration": True,
              "status": "COMPLETE", "completed": True, "provider": "azure_sdk", "voice": plan["voice"], "rate": plan["rate"], "pitch": plan["pitch"],
              "voice_status": "CANONICAL", "output_format": identity["output_format"], "bit_depth": 16,
              "timing_quality": "WORD_BOUNDARY_REAL", "same_synthesis_audio_timing": True,
              "source_file": plan["source_file"], "source_sha256": plan["source_sha256"], "beat_count": len(beats_out), "word_count": word_index - 1,
              "sample_rate": sample_rate, "channels": 1, "duration_seconds": round(len(processed) / sample_rate, 4),
              "raw_audio_file": config.relative_path(raw_combined_path), "raw_audio_sha256": _sha_file(staged_raw_combined_path),
              "audio_file": config.relative_path(output_path), "audio_sha256": _sha_file(staged_output_path), "audio_processing": processing,
              "anti_atropelamento": {"speech_overlap": 0, "overlap_allowed": False, "rule_passed": True},
              "voice_density_warnings": warnings, "beats": beats_out, "pauses": pauses,
              "cache_summary": {"benchmark_reused": benchmark_reused, "official_reused": official_reused, "azure_calls_total": azure_calls},
              "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    staged_timing_path = staging_root / "timing.json"
    _atomic_json(staged_timing_path, timing)

    commit_staged_caches()
    for staged_json, target_json in staged_metadata_upgrades:
        os.replace(staged_json, target_json)
    os.replace(staged_raw_combined_path, raw_combined_path)
    os.replace(staged_output_path, output_path)
    os.replace(staged_timing_path, timing_path)
    shutil.rmtree(staging_root, ignore_errors=True)
    return timing

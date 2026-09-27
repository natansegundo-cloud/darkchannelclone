"""Isolated, fail-closed Azure voice benchmark."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config
from .delivery import apply_delivery_cues, load_delivery_cues
from .official import OfficialNarrationError, _valid_boundaries, _validate_spoken_sequence
from .providers import ProviderError, ProviderResult
from .providers import azure_sdk
from .providers.pacing import process_voice, read_pcm_wav, write_pcm_wav
from .script_parser import parse_narration_script


DEFAULT_BEATS = ("B001", "B005", "B011", "B015", "B037")
DEFAULT_VOICES = ("pt-BR-AntonioNeural", "pt-BR-FabioNeural")
OFFICIAL_VOICE = "pt-BR-AntonioNeural"
COMPARISON_SILENCE_MS = 700


class VoiceBenchmarkError(RuntimeError):
    pass


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _voice_slug(voice: str) -> str:
    match = re.fullmatch(r"pt-BR-([A-Za-z0-9_-]+?)Neural", voice)
    value = match.group(1) if match else voice
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise VoiceBenchmarkError(f"invalid voice name: {voice!r}")
    return slug


def _run_id(value: str | None) -> str:
    result = value or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", result):
        raise VoiceBenchmarkError("run_id must contain only letters, numbers, dot, underscore or hyphen")
    return result


def _audio_stats(samples: array) -> dict[str, float | int]:
    if not samples:
        raise VoiceBenchmarkError("benchmark audio must not be empty")
    peak = max(abs(int(value)) for value in samples)
    rms = math.sqrt(sum(int(value) ** 2 for value in samples) / len(samples))

    def dbfs(value: float) -> float:
        return round(20.0 * math.log10(value / 32767.0), 4) if value else -120.0

    return {
        "sample_peak_dbfs": dbfs(peak),
        "rms_dbfs": dbfs(rms),
        "clipping_samples": sum(abs(int(value)) >= 32767 for value in samples),
    }


def apply_output_gain(samples: array, gain_db: float) -> array:
    if not math.isfinite(gain_db) or not -60.0 <= gain_db <= 0.0:
        raise VoiceBenchmarkError("output_gain_db must be finite and between -60 and 0 dB")
    multiplier = 10.0 ** (gain_db / 20.0)
    values = [round(int(value) * multiplier) for value in samples]
    if any(value < -32768 or value > 32767 for value in values):
        raise VoiceBenchmarkError("output gain would introduce clipping")
    return array("h", values)


def _gain_directory(gain_db: float) -> str:
    magnitude = f"{abs(gain_db):g}".replace(".", "_")
    direction = "minus" if gain_db < 0 else "plus"
    return f"gain_{direction}_{magnitude}db"


def _official_cache(
    official_raw_dir: Path, beat: dict[str, Any], *, episode_id: str
) -> tuple[int, array, dict[str, Any]] | None:
    wav_path = official_raw_dir / f"{beat['beat_id']}.wav"
    meta_path = official_raw_dir / f"{beat['beat_id']}.json"
    expected = {
        "episode_id": episode_id,
        "provider": "azure_sdk",
        "voice": OFFICIAL_VOICE,
        "rate": "-7%",
        "pitch": "0%",
        "beat_id": beat["beat_id"],
        "source_hash": beat["source_hash"],
        "synthesis_input_hash": beat["synthesis_input_hash"],
        "raw_text": beat["raw_text"],
    }
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if any(meta.get(key) != value for key, value in expected.items()):
            return None
        if (
            meta.get("completed") is not True
            or meta.get("timing_quality") != "WORD_BOUNDARY_REAL"
            or not meta.get("synthesis_id")
            or not _valid_boundaries(meta.get("word_boundaries"))
            or not wav_path.is_file()
            or _sha_file(wav_path) != meta.get("audio_sha256")
        ):
            return None
        sample_rate, samples = read_pcm_wav(wav_path.read_bytes())
        if sample_rate != meta.get("sample_rate") or not samples:
            return None
        _validate_spoken_sequence(beat, meta["word_boundaries"])
        return sample_rate, samples, meta
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        OfficialNarrationError,
        ProviderError,
    ):
        return None


def _isolated_cache_at(
    raw_dir: Path,
    beat: dict[str, Any],
    *,
    episode_id: str,
    voice: str,
) -> tuple[int, array, dict[str, Any]] | None:
    wav_path = raw_dir / f"{beat['beat_id']}.wav"
    meta_path = raw_dir / f"{beat['beat_id']}.json"
    expected = {
        "episode_id": episode_id,
        "provider": "azure_sdk",
        "voice": voice,
        "rate": "-7%",
        "pitch": "0%",
        "beat_id": beat["beat_id"],
        "source_hash": beat["source_hash"],
        "synthesis_input_hash": beat["synthesis_input_hash"],
        "raw_text": beat["raw_text"],
    }
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if any(meta.get(key) != value for key, value in expected.items()):
            return None
        if (
            meta.get("completed") is not True
            or meta.get("timing_quality") != "WORD_BOUNDARY_REAL"
            or not meta.get("synthesis_id")
            or not _valid_boundaries(meta.get("word_boundaries"))
            or not wav_path.is_file()
            or _sha_file(wav_path) != meta.get("audio_sha256")
        ):
            return None
        sample_rate, samples = read_pcm_wav(wav_path.read_bytes())
        if sample_rate != meta.get("sample_rate") or not samples:
            return None
        _validate_spoken_sequence(beat, meta["word_boundaries"])
        return sample_rate, samples, meta
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        OfficialNarrationError,
        ProviderError,
    ):
        return None


def _find_isolated_cache(
    benchmark_root: Path,
    slug: str,
    beat: dict[str, Any],
    *,
    episode_id: str,
    voice: str,
) -> tuple[Path, tuple[int, array, dict[str, Any]]] | None:
    if not benchmark_root.is_dir():
        return None
    for run_dir in sorted(
        (path for path in benchmark_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        raw_dir = run_dir / slug / "raw"
        cached = _isolated_cache_at(
            raw_dir, beat, episode_id=episode_id, voice=voice
        )
        if cached is not None:
            return raw_dir, cached
    return None


def plan_voice_benchmark(
    *,
    episode_id: str,
    script_path: Path,
    delivery_path: Path,
    benchmark_root: Path,
    beats: list[str] | tuple[str, ...] = DEFAULT_BEATS,
    voices: list[str] | tuple[str, ...] = DEFAULT_VOICES,
    output_gain_db: float = -3.0,
    output_gain_variants_db: list[float] | tuple[float, ...] | None = None,
    reuse_official_cache: bool = False,
    official_raw_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if episode_id != config.ACTIVE_EPISODE.episode_id:
        raise VoiceBenchmarkError("EPISODE_ID_MISMATCH: benchmark must use the active episode")
    gains = [float(value) for value in (output_gain_variants_db or (output_gain_db,))]
    if not gains or len(gains) != len(set(gains)):
        raise VoiceBenchmarkError("output gain variants must be non-empty and unique")
    for gain in gains:
        apply_output_gain(array("h", [0]), gain)
    selected_ids = list(beats)
    selected_voices = list(voices)
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise VoiceBenchmarkError("benchmark beats must be non-empty and unique")
    if not selected_voices or len(selected_voices) != len(set(selected_voices)):
        raise VoiceBenchmarkError("benchmark voices must be non-empty and unique")
    slugs = [_voice_slug(voice) for voice in selected_voices]
    if len(slugs) != len(set(slugs)):
        raise VoiceBenchmarkError("benchmark voice output names must be unique")

    parsed = parse_narration_script(script_path, source_file=config.relative_path(script_path))
    by_id = {beat["beat_id"]: beat for beat in parsed}
    unknown = [beat_id for beat_id in selected_ids if beat_id not in by_id]
    if unknown:
        raise VoiceBenchmarkError("unknown benchmark beats: " + ", ".join(unknown))
    cues = load_delivery_cues(delivery_path, parsed)
    selected_cues = {beat_id: cues[beat_id] for beat_id in selected_ids if beat_id in cues}
    raw_dir = official_raw_dir or config.DEFAULT_OUTPUT / "raw"
    voice_plans: list[dict[str, Any]] = []
    azure_calls_total = 0
    for voice, slug in zip(selected_voices, slugs):
        prepared = apply_delivery_cues(
            [by_id[beat_id] for beat_id in selected_ids],
            selected_cues,
            provider="azure_sdk",
            voice=voice,
            rate="-7%",
            pitch="0%",
        )
        entries = []
        for beat in prepared:
            official_reusable = (
                reuse_official_cache
                and voice == OFFICIAL_VOICE
                and _official_cache(raw_dir, beat, episode_id=episode_id) is not None
            )
            isolated = None if official_reusable else _find_isolated_cache(
                benchmark_root,
                slug,
                beat,
                episode_id=episode_id,
                voice=voice,
            )
            if official_reusable:
                cache_source = "official_cache"
                cache_raw_dir = None
            elif isolated is not None:
                cache_source = "benchmark_cache"
                cache_raw_dir = isolated[0]
            else:
                cache_source = "azure"
                cache_raw_dir = None
            entries.append({
                "beat": beat,
                "cache_source": cache_source,
                "cache_raw_dir": cache_raw_dir,
            })
            azure_calls_total += int(cache_source == "azure")
        voice_plans.append({"voice": voice, "slug": slug, "beats": entries})

    resolved_run_id = _run_id(run_id)
    run_dir = benchmark_root / resolved_run_id
    delivery_hash = _sha_file(delivery_path) if delivery_path.is_file() else _sha_bytes(b"")
    calls_by_voice = {
        item["voice"]: sum(entry["cache_source"] == "azure" for entry in item["beats"])
        for item in voice_plans
    }
    return {
        "episode_id": episode_id,
        "run_id": resolved_run_id,
        "run_dir": run_dir,
        "beats": selected_ids,
        "voices": selected_voices,
        "rate": "-7%",
        "pitch": "0%",
        "output_gain_db": gains[0],
        "output_gain_variants_db": gains,
        "delivery": {
            "source": config.relative_path(delivery_path),
            "sha256": delivery_hash,
        },
        "voice_plans": voice_plans,
        "azure_calls_total": azure_calls_total,
        "azure_calls_by_voice": calls_by_voice,
        "mastering_profile": {
            "highpass_hz": 70.0,
            "compressor_threshold_dbfs": -18.0,
            "compressor_ratio": 2.5,
            "peak_normalization_dbfs": -1.0,
            "limiter_ceiling_dbfs": -0.8,
            "output_gain_db": gains[0],
            "output_gain_variants_db": gains,
        },
    }


def dry_run_summary(plan: dict[str, Any]) -> dict[str, Any]:
    official_reused = {
        voice["voice"]: [
            entry["beat"]["beat_id"]
            for entry in voice["beats"]
            if entry["cache_source"] == "official_cache"
        ]
        for voice in plan["voice_plans"]
    }
    benchmark_reused = {
        voice["voice"]: [
            entry["beat"]["beat_id"]
            for entry in voice["beats"]
            if entry["cache_source"] == "benchmark_cache"
        ]
        for voice in plan["voice_plans"]
    }
    return {
        "episode_id": plan["episode_id"],
        "run_id": plan["run_id"],
        "beats": plan["beats"],
        "voices": plan["voices"],
        "official_cache_reused": official_reused,
        "benchmark_cache_reused": benchmark_reused,
        "azure_calls_by_voice": plan["azure_calls_by_voice"],
        "azure_calls_total": 0,
        "expected_real_azure_calls_total": plan["azure_calls_total"],
        "local_gain_variants_db": plan["output_gain_variants_db"],
        "official_artifacts_touched": False,
        "output_paths": {
            voice["voice"]: str(plan["run_dir"] / voice["slug"])
            for voice in plan["voice_plans"]
        },
        "manifest": str(plan["run_dir"] / "manifest.json"),
        "mastering_profile": plan["mastering_profile"],
    }


def run_voice_benchmark(
    *,
    plan: dict[str, Any],
    key: str,
    region: str,
    official_raw_dir: Path | None = None,
    synthesize: Callable[..., ProviderResult] = azure_sdk.synthesize,
    processor: Callable[[array, int], tuple[array, dict[str, Any]]] = process_voice,
) -> dict[str, Any]:
    run_dir: Path = plan["run_dir"]
    if run_dir.exists():
        raise VoiceBenchmarkError(f"benchmark run already exists: {run_dir}")
    official_dir = official_raw_dir or config.DEFAULT_OUTPUT / "raw"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan['run_id']}.", dir=run_dir.parent))
    azure_calls = 0
    voice_results: dict[str, Any] = {}
    upstream_clipping_suspected = False
    overprocessing_suspected = False
    gains = list(plan["output_gain_variants_db"])
    multi_variant = len(gains) > 1
    try:
        for voice_plan in plan["voice_plans"]:
            voice = voice_plan["voice"]
            voice_dir = staging / voice_plan["slug"]
            voice_dir.mkdir(parents=True)
            raw_dir = voice_dir / "raw"
            if multi_variant:
                raw_dir.mkdir()
            acquired: list[dict[str, Any]] = []
            for entry in voice_plan["beats"]:
                beat = entry["beat"]
                cached = None
                if entry["cache_source"] == "official_cache":
                    cached = _official_cache(official_dir, beat, episode_id=plan["episode_id"])
                    if cached is None:
                        raise VoiceBenchmarkError(
                            f"{beat['beat_id']}: official cache changed after benchmark planning"
                        )
                elif entry["cache_source"] == "benchmark_cache":
                    cached = _isolated_cache_at(
                        Path(entry["cache_raw_dir"]),
                        beat,
                        episode_id=plan["episode_id"],
                        voice=voice,
                    )
                    if cached is None:
                        raise VoiceBenchmarkError(
                            f"{beat['beat_id']}: benchmark cache changed after planning"
                        )
                if cached is not None:
                    sample_rate, raw_samples, meta = cached
                    cache_source = entry["cache_source"]
                    boundaries = meta["word_boundaries"]
                    synthesis_id = meta["synthesis_id"]
                else:
                    narrator = {
                        "voice": voice,
                        "language": "pt-BR",
                        "delivery": {"rate": "-7%", "pitch": "0%", "volume": "default"},
                    }
                    result = synthesize(beat, narrator=narrator, key=key, region=region)
                    azure_calls += 1
                    sample_rate, raw_samples = read_pcm_wav(result.audio_data)
                    boundaries = result.boundaries
                    synthesis_id = result.metadata.get("synthesis_id")
                    if (
                        result.metadata.get("timing_quality") != "WORD_BOUNDARY_REAL"
                        or not synthesis_id
                        or not _valid_boundaries(boundaries)
                    ):
                        raise VoiceBenchmarkError(
                            f"{beat['beat_id']}: invalid real WordBoundary result"
                        )
                    _validate_spoken_sequence(beat, boundaries)
                    cache_source = "azure"

                raw_stats = _audio_stats(raw_samples)
                processed, processing = processor(raw_samples, sample_rate)
                if len(processed) != len(raw_samples) or processing.get("duration_preserved") is not True:
                    raise VoiceBenchmarkError(f"{beat['beat_id']}: processing changed duration")
                before = _audio_stats(processed)
                compressor_reduction = float(processing.get("compressor_max_gain_reduction_db", 0.0))
                limiter_reduction = float(processing.get("limiter_peak_reduction_db", 0.0))
                beat_upstream_clipping = bool(
                    raw_stats["clipping_samples"] or before["clipping_samples"]
                )
                beat_overprocessing = compressor_reduction >= 12.0 or limiter_reduction >= 3.0
                upstream_clipping_suspected |= beat_upstream_clipping
                overprocessing_suspected |= beat_overprocessing
                common = {
                    "source_hash": beat["source_hash"],
                    "synthesis_input_hash": beat["synthesis_input_hash"],
                    "synthesis_id": synthesis_id,
                    "cache_source": cache_source,
                    "duration": round(len(processed) / sample_rate, 4),
                    "timing_quality": "WORD_BOUNDARY_REAL",
                    "same_synthesis_audio_timing": True,
                    "raw_sample_peak_dbfs": raw_stats["sample_peak_dbfs"],
                    "raw_rms_dbfs": raw_stats["rms_dbfs"],
                    "raw_clipping_samples": raw_stats["clipping_samples"],
                    "sample_peak_dbfs_before_output_gain": before["sample_peak_dbfs"],
                    "rms_dbfs_before_output_gain": before["rms_dbfs"],
                    "clipping_samples_before_output_gain": before["clipping_samples"],
                    "compressor_max_gain_reduction_db": compressor_reduction,
                    "limiter_peak_reduction_db": limiter_reduction,
                    "upstream_clipping_suspected": beat_upstream_clipping,
                    "overprocessing_suspected": beat_overprocessing,
                    "sample_rate": sample_rate,
                    "word_boundaries": boundaries,
                }
                if multi_variant:
                    raw_path = raw_dir / f"{beat['beat_id']}.wav"
                    write_pcm_wav(raw_path, sample_rate, raw_samples)
                    raw_meta = {
                        "episode_id": plan["episode_id"],
                        "provider": "azure_sdk",
                        "voice": voice,
                        "rate": plan["rate"],
                        "pitch": plan["pitch"],
                        "beat_id": beat["beat_id"],
                        "source_hash": beat["source_hash"],
                        "synthesis_input_hash": beat["synthesis_input_hash"],
                        "raw_text": beat["raw_text"],
                        "sample_rate": sample_rate,
                        "audio_sha256": _sha_file(raw_path),
                        "timing_quality": "WORD_BOUNDARY_REAL",
                        "synthesis_id": synthesis_id,
                        "word_boundaries": boundaries,
                        "completed": True,
                    }
                    (raw_dir / f"{beat['beat_id']}.json").write_text(
                        json.dumps(raw_meta, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    common["raw_audio_sha256"] = raw_meta["audio_sha256"]
                    common["raw_audio_file"] = f"{voice_plan['slug']}/raw/{beat['beat_id']}.wav"
                acquired.append({
                    "beat": beat,
                    "sample_rate": sample_rate,
                    "processed": processed,
                    "common": common,
                })

            rates = {item["sample_rate"] for item in acquired}
            if len(rates) != 1:
                raise VoiceBenchmarkError("sample-rate mismatch between benchmark beats")
            comparison_rate = rates.pop()
            variants: dict[str, Any] = {}
            for gain in gains:
                variant_name = _gain_directory(gain) if multi_variant else voice_plan["slug"]
                variant_dir = voice_dir / variant_name if multi_variant else voice_dir
                if multi_variant:
                    variant_dir.mkdir()
                comparison = array("h")
                beat_results: dict[str, Any] = {}
                for index, item in enumerate(acquired):
                    beat = item["beat"]
                    mastered = apply_output_gain(item["processed"], gain)
                    after = _audio_stats(mastered)
                    target = variant_dir / f"{beat['beat_id']}.wav"
                    write_pcm_wav(target, comparison_rate, mastered)
                    if index:
                        comparison.extend(
                            array("h", [0])
                            * round(comparison_rate * COMPARISON_SILENCE_MS / 1000)
                        )
                    comparison.extend(mastered)
                    relative = target.relative_to(staging).as_posix()
                    beat_results[beat["beat_id"]] = {
                        **item["common"],
                        "output_gain_db": gain,
                        "audio_sha256": _sha_file(target),
                        "sample_peak_dbfs": after["sample_peak_dbfs"],
                        "rms_dbfs": after["rms_dbfs"],
                        "clipping_samples_after_output_gain": after["clipping_samples"],
                        "output_file": relative,
                    }
                comparison_path = variant_dir / "comparison.wav"
                write_pcm_wav(comparison_path, comparison_rate, comparison)
                variants[variant_name] = {
                    "output_gain_db": gain,
                    "comparison_audio_sha256": _sha_file(comparison_path),
                    "comparison_duration": round(len(comparison) / comparison_rate, 4),
                    "beats": beat_results,
                }
            if multi_variant:
                voice_results[voice] = {
                    "output_directory": voice_plan["slug"],
                    "raw_directory": f"{voice_plan['slug']}/raw",
                    "variants": variants,
                }
            else:
                only = variants[voice_plan["slug"]]
                voice_results[voice] = {
                    "output_directory": voice_plan["slug"],
                    "comparison_audio_sha256": only["comparison_audio_sha256"],
                    "comparison_duration": only["comparison_duration"],
                    "beats": only["beats"],
                }

        manifest = {
            "schema_version": "1.0",
            "episode_id": plan["episode_id"],
            "run_id": plan["run_id"],
            "beats": plan["beats"],
            "voices": plan["voices"],
            "rate": plan["rate"],
            "pitch": plan["pitch"],
            "output_gain_db": plan["output_gain_db"],
            "output_gain_variants_db": gains,
            "delivery": plan["delivery"],
            "mastering_profile": plan["mastering_profile"],
            "comparison_silence_ms": COMPARISON_SILENCE_MS,
            "azure_calls_total": azure_calls,
            "upstream_clipping_suspected": upstream_clipping_suspected,
            "overprocessing_suspected": overprocessing_suspected,
            "voice_results": voice_results,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        staging.replace(run_dir)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

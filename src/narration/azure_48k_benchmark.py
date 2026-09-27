"""Raw-only Azure Standard voice benchmark at native 48 kHz PCM."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import config
from .delivery import apply_delivery_cues, load_delivery_cues
from .official import OfficialNarrationError, _valid_boundaries, _validate_spoken_sequence
from .providers import ProviderError, ProviderResult
from .providers import azure_sdk
from .providers.pacing import read_pcm_wav, write_pcm_wav
from .script_parser import parse_narration_script


DEFAULT_BEATS = ("B001", "B005", "B037")
VOICES = (
    ("pt-BR-HumbertoNeural", "humberto"),
    ("pt-BR-DonatoNeural", "donato"),
    ("pt-BR-ValerioNeural", "valerio"),
)
OUTPUT_SAMPLE_RATE = 48_000
OUTPUT_FORMAT = "RIFF 48KHZ 16BIT MONO PCM"
COMPARISON_SILENCE_MS = 700


class Azure48kBenchmarkError(RuntimeError):
    pass


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _wav_metrics(value: bytes) -> tuple[int, int, array, dict[str, float]]:
    try:
        with wave.open(__import__("io").BytesIO(value), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise Azure48kBenchmarkError("Azure WAV must be 16-bit mono PCM")
            rate = handle.getframerate()
            samples = array("h")
            samples.frombytes(handle.readframes(handle.getnframes()))
    except (wave.Error, EOFError) as exc:
        raise Azure48kBenchmarkError(f"invalid Azure WAV: {exc}") from exc
    if not samples:
        raise Azure48kBenchmarkError("Azure WAV is empty")
    peak = max(abs(int(sample)) for sample in samples)
    rms = math.sqrt(sum(int(sample) ** 2 for sample in samples) / len(samples))

    def dbfs(level: float) -> float:
        return round(20.0 * math.log10(level / 32767.0), 4) if level else -120.0

    return rate, 16, samples, {
        "peak_dbfs": dbfs(peak),
        "rms_dbfs": dbfs(rms),
    }


def _benchmark_hash(beat: dict[str, Any], voice: str) -> str:
    value = {
        "synthesis_input_hash": beat["synthesis_input_hash"],
        "voice": voice,
        "rate": "-7%",
        "pitch": "0%",
        "output_format": "riff-48khz-16bit-mono-pcm",
    }
    return _sha_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _cache_at(
    voice_dir: Path,
    beat: dict[str, Any],
    *,
    voice: str,
) -> tuple[bytes, dict[str, Any]] | None:
    wav_path = voice_dir / f"{beat['beat_id']}.wav"
    metadata_path = voice_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        row = metadata["beats"][beat["beat_id"]]
        expected = {
            "voice": voice,
            "source_hash": beat["source_hash"],
            "synthesis_input_hash": beat["synthesis_input_hash"],
            "benchmark_input_hash": beat["benchmark_input_hash"],
            "sample_rate": OUTPUT_SAMPLE_RATE,
            "bit_depth": 16,
            "timing_quality": "WORD_BOUNDARY_REAL",
            "same_synthesis_audio_timing": True,
        }
        if any(row.get(key) != expected_value for key, expected_value in expected.items()):
            return None
        if (
            not row.get("synthesis_id")
            or not _valid_boundaries(row.get("word_boundaries"))
            or not wav_path.is_file()
            or _sha_file(wav_path) != row.get("audio_sha256")
        ):
            return None
        audio = wav_path.read_bytes()
        rate, depth, _samples, _metrics = _wav_metrics(audio)
        if rate != OUTPUT_SAMPLE_RATE or depth != 16:
            return None
        _validate_spoken_sequence(beat, row["word_boundaries"])
        return audio, row
    except (
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
        OfficialNarrationError,
        ProviderError,
    ):
        return None


def _find_cache(
    benchmark_root: Path,
    slug: str,
    beat: dict[str, Any],
    *,
    voice: str,
) -> tuple[Path, bytes, dict[str, Any]] | None:
    if not benchmark_root.is_dir():
        return None
    for run_dir in sorted(
        (path for path in benchmark_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        voice_dir = run_dir / slug
        cached = _cache_at(voice_dir, beat, voice=voice)
        if cached is not None:
            return voice_dir, *cached
    return None


def build_plan(
    *,
    episode_id: str,
    script_path: Path,
    delivery_path: Path,
    benchmark_root: Path,
    beats: Sequence[str] = DEFAULT_BEATS,
    run_id: str | None = None,
) -> dict[str, Any]:
    if episode_id != config.ACTIVE_EPISODE.episode_id:
        raise Azure48kBenchmarkError("EPISODE_ID_MISMATCH")
    selected_ids = list(beats)
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise Azure48kBenchmarkError("beats must be non-empty and unique")
    parsed = parse_narration_script(script_path, source_file=config.relative_path(script_path))
    by_id = {beat["beat_id"]: beat for beat in parsed}
    unknown = [beat_id for beat_id in selected_ids if beat_id not in by_id]
    if unknown:
        raise Azure48kBenchmarkError("unknown beats: " + ", ".join(unknown))
    cues = load_delivery_cues(delivery_path, parsed)
    selected_cues = {beat_id: cues[beat_id] for beat_id in selected_ids if beat_id in cues}
    voice_plans = []
    expected_calls = 0
    for voice, slug in VOICES:
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
            beat["benchmark_input_hash"] = _benchmark_hash(beat, voice)
            cached = _find_cache(benchmark_root, slug, beat, voice=voice)
            entries.append({
                "beat": beat,
                "cache_source": "azure_48k_cache" if cached else "azure",
                "cache_voice_dir": cached[0] if cached else None,
            })
            expected_calls += int(cached is None)
        voice_plans.append({"voice": voice, "slug": slug, "beats": entries})
    identifier = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", identifier):
        raise Azure48kBenchmarkError("invalid run_id")
    return {
        "episode_id": episode_id,
        "run_id": identifier,
        "run_dir": benchmark_root / identifier,
        "beats": selected_ids,
        "voices": voice_plans,
        "format": OUTPUT_FORMAT,
        "expected_azure_calls": expected_calls,
        "raw_processing": "NONE",
    }


def dry_run_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "episode_id": plan["episode_id"],
        "run_id": plan["run_id"],
        "format": plan["format"],
        "voices": [voice["voice"] for voice in plan["voices"]],
        "beats": plan["beats"],
        "cache_reused": {
            voice["voice"]: [
                entry["beat"]["beat_id"]
                for entry in voice["beats"]
                if entry["cache_source"] == "azure_48k_cache"
            ]
            for voice in plan["voices"]
        },
        "expected_azure_calls": plan["expected_azure_calls"],
        "azure_calls_total": 0,
        "raw_processing": "NONE",
        "output_paths": {
            voice["voice"]: str(plan["run_dir"] / voice["slug"])
            for voice in plan["voices"]
        },
        "official_artifacts_touched": False,
    }


def run_benchmark(
    *,
    plan: dict[str, Any],
    key: str,
    region: str,
    synthesize: Callable[..., ProviderResult] = azure_sdk.synthesize,
) -> dict[str, Any]:
    run_dir = Path(plan["run_dir"])
    if run_dir.exists():
        raise Azure48kBenchmarkError(f"benchmark run already exists: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan['run_id']}-", dir=run_dir.parent))
    azure_calls = 0
    voice_results: dict[str, Any] = {}
    try:
        for voice_plan in plan["voices"]:
            voice = voice_plan["voice"]
            voice_dir = staging / voice_plan["slug"]
            voice_dir.mkdir()
            comparison = array("h")
            beat_results: dict[str, Any] = {}
            for index, entry in enumerate(voice_plan["beats"]):
                beat = entry["beat"]
                if entry["cache_source"] == "azure_48k_cache":
                    cached = _cache_at(Path(entry["cache_voice_dir"]), beat, voice=voice)
                    if cached is None:
                        raise Azure48kBenchmarkError(
                            f"{voice}/{beat['beat_id']}: cache changed after planning"
                        )
                    audio, cached_row = cached
                    boundaries = cached_row["word_boundaries"]
                    synthesis_id = cached_row["synthesis_id"]
                    cache_source = "azure_48k_cache"
                else:
                    narrator = {
                        "voice": voice,
                        "language": "pt-BR",
                        "delivery": {"rate": "-7%", "pitch": "0%", "volume": "default"},
                    }
                    result = synthesize(
                        beat,
                        narrator=narrator,
                        key=key,
                        region=region,
                        output_sample_rate=OUTPUT_SAMPLE_RATE,
                    )
                    azure_calls += 1
                    if (
                        result.metadata.get("timing_quality") != "WORD_BOUNDARY_REAL"
                        or not result.metadata.get("synthesis_id")
                        or not _valid_boundaries(result.boundaries)
                    ):
                        raise Azure48kBenchmarkError(
                            f"{voice}/{beat['beat_id']}: invalid WordBoundary result"
                        )
                    _validate_spoken_sequence(beat, result.boundaries)
                    audio = result.audio_data
                    boundaries = result.boundaries
                    synthesis_id = result.metadata["synthesis_id"]
                    cache_source = "azure"
                rate, depth, samples, metrics = _wav_metrics(audio)
                if rate != OUTPUT_SAMPLE_RATE or depth != 16:
                    raise Azure48kBenchmarkError(
                        f"{voice}/{beat['beat_id']}: expected 48000 Hz 16-bit PCM"
                    )
                target = voice_dir / f"{beat['beat_id']}.wav"
                target.write_bytes(audio)
                if index:
                    comparison.extend(
                        array("h", [0])
                        * round(OUTPUT_SAMPLE_RATE * COMPARISON_SILENCE_MS / 1000)
                    )
                comparison.extend(samples)
                beat_results[beat["beat_id"]] = {
                    "voice": voice,
                    "source_hash": beat["source_hash"],
                    "synthesis_input_hash": beat["synthesis_input_hash"],
                    "benchmark_input_hash": beat["benchmark_input_hash"],
                    "delivery_cues": beat.get("delivery_cues", []),
                    "sample_rate": rate,
                    "bit_depth": depth,
                    "duration": round(len(samples) / rate, 4),
                    "audio_sha256": _sha_bytes(audio),
                    "peak_dbfs": metrics["peak_dbfs"],
                    "rms_dbfs": metrics["rms_dbfs"],
                    "timing_quality": "WORD_BOUNDARY_REAL",
                    "same_synthesis_audio_timing": True,
                    "synthesis_id": synthesis_id,
                    "word_boundaries": boundaries,
                    "cache_source": cache_source,
                    "raw_processing": "NONE",
                }
            comparison_path = voice_dir / "comparison.wav"
            write_pcm_wav(comparison_path, OUTPUT_SAMPLE_RATE, comparison)
            metadata = {
                "schema_version": "1.0",
                "episode_id": plan["episode_id"],
                "voice": voice,
                "format": OUTPUT_FORMAT,
                "rate": "-7%",
                "pitch": "0%",
                "raw_processing": "NONE",
                "comparison_silence_ms": COMPARISON_SILENCE_MS,
                "comparison_audio_sha256": _sha_file(comparison_path),
                "beats": beat_results,
            }
            (voice_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            voice_results[voice] = metadata
        manifest = {
            "schema_version": "1.0",
            "episode_id": plan["episode_id"],
            "run_id": plan["run_id"],
            "scope": "azure_standard_48k_raw_benchmark",
            "format": OUTPUT_FORMAT,
            "voices": [voice for voice, _slug in VOICES],
            "beats": plan["beats"],
            "raw_processing": "NONE",
            "azure_calls_total": azure_calls,
            "voice_results": voice_results,
            "official_artifacts_touched": False,
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

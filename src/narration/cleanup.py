"""Local Humberto hiss diagnosis and conservative cleanup variants."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import config
from .benchmark import (
    COMPARISON_SILENCE_MS,
    VoiceBenchmarkError,
    _audio_stats,
    _isolated_cache_at,
    apply_output_gain,
)
from .delivery import apply_delivery_cues, load_delivery_cues
from .providers.pacing import process_voice, read_pcm_wav, write_pcm_wav
from .script_parser import parse_narration_script


VOICE = "pt-BR-HumbertoNeural"
DEFAULT_BEATS = ("B001", "B005", "B011", "B015", "B037")
OUTPUT_GAIN_DB = -9.0
LOWPASS_HZ = 10_500
DENOISE_FILTER = "afftdn=nr=4:nf=-55:tn=1"
LOWPASS_FILTER = f"lowpass=f={LOWPASS_HZ}:p=1"
VARIANTS = {
    "baseline": None,
    "gentle_denoise": DENOISE_FILTER,
    "gentle_lowpass": LOWPASS_FILTER,
    "gentle_denoise_lowpass": f"{DENOISE_FILTER},{LOWPASS_FILTER}",
}


class CleanupError(RuntimeError):
    pass


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dbfs(value: float) -> float:
    return round(20.0 * math.log10(value / 32767.0), 4) if value else -120.0


def _high_frequency_metrics(samples: array, sample_rate: int) -> dict[str, float]:
    """Measure a stable one-pole high-pass proxy above the hiss region."""

    cutoff_hz = 8_000.0
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    dt = 1.0 / sample_rate
    alpha = rc / (rc + dt)
    previous_input = 0.0
    previous_output = 0.0
    high_energy = 0.0
    total_energy = 0.0
    for sample in samples:
        value = float(sample)
        highpassed = alpha * (previous_output + value - previous_input)
        previous_input = value
        previous_output = highpassed
        high_energy += highpassed * highpassed
        total_energy += value * value
    high_rms = math.sqrt(high_energy / max(1, len(samples)))
    ratio_db = (
        10.0 * math.log10(high_energy / total_energy)
        if high_energy and total_energy
        else -120.0
    )
    return {
        "high_frequency_cutoff_hz": cutoff_hz,
        "high_frequency_rms_dbfs": _dbfs(high_rms),
        "high_frequency_energy_ratio_db": round(ratio_db, 4),
    }


def _silence_intervals(
    boundaries: Sequence[dict[str, Any]], sample_rate: int, sample_count: int
) -> list[tuple[int, int]]:
    occupied = sorted(
        (
            max(0, round(float(item["audio_offset_ms"]) * sample_rate / 1000)),
            min(
                sample_count,
                round(
                    (float(item["audio_offset_ms"]) + float(item.get("duration_ms") or 0))
                    * sample_rate
                    / 1000
                ),
            ),
        )
        for item in boundaries
    )
    minimum = round(sample_rate * 0.02)
    intervals: list[tuple[int, int]] = []
    cursor = 0
    for start, end in occupied:
        if start - cursor >= minimum:
            intervals.append((cursor, start))
        cursor = max(cursor, end)
    if sample_count - cursor >= minimum:
        intervals.append((cursor, sample_count))
    return intervals


def _estimated_noise_floor(
    samples: array, sample_rate: int, boundaries: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    frame = max(1, round(sample_rate * 0.02))
    intervals = _silence_intervals(boundaries, sample_rate, len(samples))
    frame_rms: list[float] = []
    for start, end in intervals:
        for cursor in range(start, end - frame + 1, frame):
            values = samples[cursor : cursor + frame]
            frame_rms.append(math.sqrt(sum(int(value) ** 2 for value in values) / len(values)))
    method = "word_boundary_silence_quiet_quartile"
    if not frame_rms:
        method = "whole_audio_quiet_decile_fallback"
        for cursor in range(0, len(samples) - frame + 1, frame):
            values = samples[cursor : cursor + frame]
            frame_rms.append(math.sqrt(sum(int(value) ** 2 for value in values) / len(values)))
    nonzero_frames = [value for value in frame_rms if value > 0]
    ordered = sorted(nonzero_frames or frame_rms)
    if nonzero_frames:
        method += "_excluding_digital_zero"
    quiet_count = max(1, math.ceil(len(ordered) * 0.25))
    estimate = sum(ordered[:quiet_count]) / quiet_count if ordered else 0.0
    return {
        "estimated_noise_floor_dbfs": _dbfs(estimate),
        "noise_floor_method": method,
        "silence_interval_count": len(intervals),
        "silence_frame_count": len(frame_rms),
    }


def measure_audio(
    samples: array,
    sample_rate: int,
    boundaries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **_audio_stats(samples),
        **_estimated_noise_floor(samples, sample_rate, boundaries),
        **_high_frequency_metrics(samples, sample_rate),
        "duration": round(len(samples) / sample_rate, 4),
    }


def _resolve_ffmpeg(value: str | Path) -> Path:
    requested = Path(value)
    if requested.is_file():
        return requested.resolve()
    found = shutil.which(str(value))
    if found:
        return Path(found).resolve()
    if str(value).casefold() == "ffmpeg" and os.name == "nt":
        packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
        matches = sorted(packages.glob("Gyan.FFmpeg.Essentials_*/*/bin/ffmpeg.exe"), reverse=True)
        if matches:
            return matches[0].resolve()
    raise CleanupError(f"FFmpeg is unavailable: {value}")


def _filter_audio(
    source: Path,
    target: Path,
    filter_graph: str,
    *,
    ffmpeg: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-af", filter_graph,
        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target),
    ]
    result = runner(command, capture_output=True, text=True, shell=False)
    if result.returncode != 0 or not target.is_file():
        detail = (result.stderr or result.stdout or "FFmpeg produced no output").strip()[-1000:]
        raise CleanupError(f"FFmpeg cleanup failed: {detail}")


def find_humberto_source(
    benchmark_root: Path,
    *,
    beats: Sequence[str] = DEFAULT_BEATS,
) -> Path:
    if not benchmark_root.is_dir():
        raise CleanupError("voice benchmark directory does not exist")
    for run_dir in sorted(
        (path for path in benchmark_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        raw_dir = run_dir / "humberto" / "raw"
        if all((raw_dir / f"{beat}.wav").is_file() and (raw_dir / f"{beat}.json").is_file() for beat in beats):
            return raw_dir
    raise CleanupError("no complete Humberto raw benchmark cache was found")


def _run_id(value: str | None) -> str:
    result = value or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", result):
        raise CleanupError("run_id contains invalid characters")
    return result


def run_humberto_cleanup(
    *,
    source_raw_dir: Path,
    benchmark_root: Path,
    script_path: Path,
    delivery_path: Path,
    episode_id: str,
    beats: Sequence[str] = DEFAULT_BEATS,
    run_id: str | None = None,
    ffmpeg: str | Path = "ffmpeg",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if episode_id != config.ACTIVE_EPISODE.episode_id:
        raise CleanupError("EPISODE_ID_MISMATCH: cleanup must use the active episode")
    ffmpeg_path = _resolve_ffmpeg(ffmpeg)
    parsed = parse_narration_script(script_path, source_file=config.relative_path(script_path))
    by_id = {beat["beat_id"]: beat for beat in parsed}
    cues = load_delivery_cues(delivery_path, parsed)
    selected_cues = {beat_id: cues[beat_id] for beat_id in beats if beat_id in cues}
    prepared = apply_delivery_cues(
        [by_id[beat_id] for beat_id in beats],
        selected_cues,
        provider="azure_sdk",
        voice=VOICE,
        rate="-7%",
        pitch="0%",
    )
    acquired: list[dict[str, Any]] = []
    for beat in prepared:
        cached = _isolated_cache_at(
            source_raw_dir, beat, episode_id=episode_id, voice=VOICE
        )
        if cached is None:
            raise CleanupError(f"{beat['beat_id']}: Humberto raw cache is invalid")
        sample_rate, raw_samples, meta = cached
        acquired.append({
            "beat": beat,
            "sample_rate": sample_rate,
            "raw_samples": raw_samples,
            "meta": meta,
            "raw_measurements": measure_audio(raw_samples, sample_rate, meta["word_boundaries"]),
        })

    resolved_run_id = _run_id(run_id)
    output_dir = benchmark_root / resolved_run_id / "humberto_cleanup"
    if output_dir.exists():
        raise CleanupError(f"cleanup run already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{resolved_run_id}-cleanup-", dir=output_dir.parent))
    staging = staging_root / "humberto_cleanup"
    staging.mkdir()
    variant_results: dict[str, Any] = {}
    baseline_measurements: dict[str, dict[str, Any]] = {}
    try:
        for variant, filter_graph in VARIANTS.items():
            variant_dir = staging / variant
            variant_dir.mkdir()
            comparison = array("h")
            comparison_rate: int | None = None
            beats_out: dict[str, Any] = {}
            experimental_reasons: list[str] = []
            for index, item in enumerate(acquired):
                beat = item["beat"]
                sample_rate = item["sample_rate"]
                filtered = item["raw_samples"]
                with tempfile.TemporaryDirectory(prefix="cleanup-filter-", dir=staging_root) as temp_name:
                    if filter_graph:
                        source_path = source_raw_dir / f"{beat['beat_id']}.wav"
                        filtered_path = Path(temp_name) / "filtered.wav"
                        _filter_audio(
                            source_path, filtered_path, filter_graph,
                            ffmpeg=ffmpeg_path, runner=runner,
                        )
                        filtered_rate, filtered = read_pcm_wav(filtered_path.read_bytes())
                        if filtered_rate != sample_rate or len(filtered) != len(item["raw_samples"]):
                            raise CleanupError(f"{variant}/{beat['beat_id']}: cleanup changed duration or sample rate")
                processed, processing = process_voice(filtered, sample_rate)
                mastered = apply_output_gain(processed, OUTPUT_GAIN_DB)
                if len(mastered) != len(item["raw_samples"]):
                    raise CleanupError(f"{variant}/{beat['beat_id']}: processing changed duration")
                measurements = measure_audio(mastered, sample_rate, item["meta"]["word_boundaries"])
                if measurements["clipping_samples"]:
                    raise CleanupError(f"{variant}/{beat['beat_id']}: clipping detected")
                target = variant_dir / f"{beat['beat_id']}.wav"
                write_pcm_wav(target, sample_rate, mastered)
                if comparison_rate is None:
                    comparison_rate = sample_rate
                elif comparison_rate != sample_rate:
                    raise CleanupError("sample-rate mismatch between cleanup beats")
                if index:
                    comparison.extend(array("h", [0]) * round(sample_rate * COMPARISON_SILENCE_MS / 1000))
                comparison.extend(mastered)
                row = {
                    "raw_audio_sha256": item["meta"]["audio_sha256"],
                    "source_hash": beat["source_hash"],
                    "synthesis_input_hash": beat["synthesis_input_hash"],
                    "synthesis_id": item["meta"]["synthesis_id"],
                    "timing_quality": "WORD_BOUNDARY_REAL",
                    "same_synthesis_audio_timing": True,
                    "output_gain_db": OUTPUT_GAIN_DB,
                    "filter_graph": filter_graph or "none",
                    "audio_sha256": _sha_file(target),
                    "output_file": target.relative_to(staging_root).as_posix(),
                    "raw": item["raw_measurements"],
                    "processed": measurements,
                    "high_frequency_energy_before": item["raw_measurements"]["high_frequency_energy_ratio_db"],
                    "high_frequency_energy_after": measurements["high_frequency_energy_ratio_db"],
                    "audio_processing": processing,
                }
                beats_out[beat["beat_id"]] = row
                if variant == "baseline":
                    baseline_measurements[beat["beat_id"]] = measurements
                elif "denoise" in variant:
                    baseline = baseline_measurements[beat["beat_id"]]
                    if measurements["estimated_noise_floor_dbfs"] > baseline["estimated_noise_floor_dbfs"] + 1.5:
                        experimental_reasons.append(f"{beat['beat_id']}: estimated noise floor increased")
                    if abs(measurements["rms_dbfs"] - baseline["rms_dbfs"]) > 1.5:
                        experimental_reasons.append(f"{beat['beat_id']}: RMS changed by more than 1.5 dB")
            if comparison_rate is None:
                raise CleanupError(f"no audio produced for {variant}")
            comparison_path = variant_dir / "comparison.wav"
            write_pcm_wav(comparison_path, comparison_rate, comparison)
            variant_results[variant] = {
                "status": "EXPERIMENTAL" if experimental_reasons else "CANDIDATE",
                "experimental_reasons": experimental_reasons,
                "filter_graph": filter_graph or "none",
                "comparison_audio_sha256": _sha_file(comparison_path),
                "comparison_duration": round(len(comparison) / comparison_rate, 4),
                "beats": beats_out,
            }

        raw_noise = [item["raw_measurements"]["estimated_noise_floor_dbfs"] for item in acquired]
        raw_hf = [item["raw_measurements"]["high_frequency_energy_ratio_db"] for item in acquired]
        baseline_rows = variant_results["baseline"]["beats"].values()
        processed_noise = [row["processed"]["estimated_noise_floor_dbfs"] for row in baseline_rows]
        baseline_rows = variant_results["baseline"]["beats"].values()
        processed_hf = [row["processed"]["high_frequency_energy_ratio_db"] for row in baseline_rows]
        raw_noise_avg = sum(raw_noise) / len(raw_noise)
        raw_hf_avg = sum(raw_hf) / len(raw_hf)
        processed_noise_avg = sum(processed_noise) / len(processed_noise)
        processed_hf_avg = sum(processed_hf) / len(processed_hf)
        if raw_noise_avg >= -55 and raw_hf_avg >= -35:
            hiss_raw = "LIKELY"
        elif raw_noise_avg <= -70 and raw_hf_avg <= -45:
            hiss_raw = "NO"
        else:
            hiss_raw = "INCONCLUSIVE"
        noise_delta = processed_noise_avg - raw_noise_avg
        hf_delta = processed_hf_avg - raw_hf_avg
        if noise_delta >= 1.0 and hf_delta >= 1.0:
            hiss_amplified = "LIKELY"
        elif noise_delta <= 0.5 and hf_delta <= 0.5:
            hiss_amplified = "NO"
        else:
            hiss_amplified = "INCONCLUSIVE"
        variant_floor_averages = {
            name: sum(
                row["processed"]["estimated_noise_floor_dbfs"]
                for row in variant_results[name]["beats"].values()
            ) / len(acquired)
            for name in variant_results
        }
        best_floor = min(variant_floor_averages.values())
        tied = [
            name for name, value in variant_floor_averages.items()
            if abs(value - best_floor) < 0.1
        ]
        best_variant = tied[0] if len(tied) == 1 else "TIE: " + ", ".join(tied)
        manifest = {
            "schema_version": "1.0",
            "episode_id": episode_id,
            "run_id": resolved_run_id,
            "scope": "humberto_local_cleanup_benchmark",
            "voice": VOICE,
            "voice_status": "APPROVED_CANDIDATE",
            "fabio_status": "REJECTED",
            "beats": list(beats),
            "source_raw_directory": config.relative_path(source_raw_dir),
            "azure_calls_total": 0,
            "output_gain_db": OUTPUT_GAIN_DB,
            "lowpass_hz": LOWPASS_HZ,
            "ffmpeg": {"path": str(ffmpeg_path), "denoise_filter": DENOISE_FILTER},
            "hiss_present_in_raw": hiss_raw,
            "hiss_amplified_by_processing": hiss_amplified,
            "diagnostic_averages": {
                "raw_noise_floor_dbfs": round(raw_noise_avg, 4),
                "processed_noise_floor_dbfs": round(processed_noise_avg, 4),
                "noise_floor_delta_db": round(noise_delta, 4),
                "raw_high_frequency_energy_ratio_db": round(raw_hf_avg, 4),
                "processed_high_frequency_energy_ratio_db": round(processed_hf_avg, 4),
                "high_frequency_energy_delta_db": round(hf_delta, 4),
            },
            "best_measured_noise_floor": {
                "variant": best_variant,
                "average_dbfs": round(best_floor, 4),
            },
            "variants": variant_results,
            "official_artifacts_touched": False,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        staging.replace(output_dir)
        shutil.rmtree(staging_root, ignore_errors=True)
        return manifest
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

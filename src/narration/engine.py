"""Orquestração única de narração, timing e pós-processamento."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config
from .providers import ProviderError, ProviderResult
from .providers import azure_sdk, local_kokoro
from .providers.pacing import (
    BEATS_DATA,
    BEATS_METADATA,
    align_words,
    beats_for,
    process_voice,
    read_pcm_wav,
    write_pcm_wav,
)
from .script_parser import NarrationScriptError, parse_narration_script
from .delivery import DeliveryCueError, apply_delivery_cues, load_delivery_cues
from .official import OfficialNarrationError, preserve_official_rollback, run_official_narration
from .benchmark import (
    DEFAULT_BEATS as BENCHMARK_DEFAULT_BEATS,
    DEFAULT_VOICES as BENCHMARK_DEFAULT_VOICES,
    VoiceBenchmarkError,
    dry_run_summary,
    plan_voice_benchmark,
    run_voice_benchmark,
)
from .cleanup import (
    DEFAULT_BEATS as CLEANUP_DEFAULT_BEATS,
    CleanupError,
    find_humberto_source,
    run_humberto_cleanup,
)
from .azure_48k_benchmark import (
    DEFAULT_BEATS as AZURE_48K_DEFAULT_BEATS,
    Azure48kBenchmarkError,
    build_plan as build_azure_48k_plan,
    dry_run_summary as azure_48k_dry_run_summary,
    run_benchmark as run_azure_48k_benchmark,
)
from .validation import (
    apply_operational_delivery,
    validate_delivery_contract,
    validate_pilot_narration,
)


class NarrationError(RuntimeError):
    """Falha pública do engine consolidado."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _provider_name(value: str) -> str:
    aliases = {"azure": "azure_sdk", "kokoro": "local_kokoro", "local": "local_kokoro"}
    return aliases.get(value.casefold(), value.casefold())


def _provider_function(name: str) -> Callable[..., ProviderResult]:
    if name == "azure_sdk":
        return azure_sdk.synthesize
    if name == "local_kokoro":
        return local_kokoro.synthesize
    raise NarrationError(f"Provider não suportado: {name}")


def _sdk_words(
    boundaries: list[dict[str, Any]],
    *,
    offset_seconds: float,
    global_index: int,
) -> tuple[list[dict[str, Any]], float, float]:
    if not boundaries:
        raise NarrationError("Provider SDK não retornou WordBoundary.")
    words: list[dict[str, Any]] = []
    for beat_word_index, boundary in enumerate(boundaries, start=1):
        start = offset_seconds + float(boundary["audio_offset_ms"]) / 1000.0
        duration = float(boundary.get("duration_ms") or 0.0) / 1000.0
        words.append(
            {
                "index": global_index + beat_word_index - 1,
                "beat_word_index": beat_word_index,
                "text": boundary["text"],
                "normalized": boundary["normalized"],
                "start": round(start, 4),
                "end": round(start + duration, 4),
                "audio_offset_ms": boundary["audio_offset_ms"],
                "duration_ms": boundary["duration_ms"],
                "boundary_type": boundary["boundary_type"],
                "text_offset": boundary["text_offset"],
                "word_length": boundary["word_length"],
            }
        )
    return words, words[0]["start"], words[-1]["end"]


def _select_beats(selection: str | None, pacing: str) -> list[dict[str, Any]]:
    if pacing == "v2":
        return beats_for(selection)
    # O modo legado mantém a unidade canônica de beats, mas usa a entrega
    # configurada no narrador. A seleção e a saída permanecem compatíveis.
    return beats_for(selection)


def build_official_narration_plan(
    *,
    input_path: Path,
    output_path: Path,
    episode_id: str,
    narrator_id: str | None = None,
) -> dict[str, Any]:
    """Compile the active episode's official script without synthesis."""

    if episode_id != config.ACTIVE_EPISODE.episode_id:
        raise NarrationError(
            "EPISODE_ID_MISMATCH: official narration must use the active episode"
        )
    if input_path.resolve() != config.DEFAULT_INPUT.resolve():
        raise NarrationError(
            "official narration source must be the active episode roteiro_narracao.md"
        )
    narrators = config.load_narrators()
    narrator = config.narrator_by_id(
        narrators, narrator_id or narrators["default_narrator"]
    )
    contract = config.load_motion_contract()
    config_errors = validate_delivery_contract(narrators, contract)
    if config_errors:
        raise NarrationError(
            "Configuração de narração inválida: " + "; ".join(config_errors)
        )
    narrator = apply_operational_delivery(narrator, contract)
    azure = narrator.get("azure", {})
    output_format = azure.get("output_format")
    if output_format != "riff-48khz-16bit-mono-pcm":
        raise NarrationError("Official narration requires native RIFF 48 kHz 16-bit mono PCM")
    source_file = config.relative_path(input_path)
    beats = parse_narration_script(input_path, source_file=source_file)
    cues = load_delivery_cues(config.ACTIVE_EPISODE.file("narration_delivery.json"), beats)
    beats = apply_delivery_cues(
        beats,
        cues,
        provider="azure_sdk",
        voice=narrator["voice"],
        rate=narrator["delivery"]["rate"],
        pitch=narrator["delivery"]["pitch"],
        output_format=output_format,
    )
    plan = {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "scope": "official_narration",
        "official_narration": True,
        "source_file": source_file,
        "source_sha256": _sha256(input_path),
        "beat_count": len(beats),
        "word_count": sum(int(beat["word_count"]) for beat in beats),
        "voice": narrator["voice"],
        "rate": narrator["delivery"]["rate"],
        "pitch": narrator["delivery"]["pitch"],
        "output_format": output_format,
        "sample_rate": int(azure.get("sample_rate", 0)),
        "bit_depth": int(azure.get("bit_depth", 0)),
        "channels": int(azure.get("channels", 0)),
        "beats": beats,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return plan


def run_narration(
    *,
    input_path: Path,
    beats: str | None,
    output_path: Path,
    timing_path: Path,
    raw_dir: Path | None = None,
    raw_combined_path: Path | None = None,
    episode_id: str = config.ACTIVE_EPISODE.episode_id,
    narrator_id: str | None = None,
    provider: str = "azure_sdk",
    env_file: Path = config.DEFAULT_ENV_PATH,
    pause_ms: int | None = None,
    no_processing: bool = False,
    voice_pacing: str = "v2",
) -> dict[str, Any]:
    if not input_path.is_file():
        raise NarrationError(f"Roteiro não encontrado: {input_path}")
    if voice_pacing not in {"v2", "legado"}:
        raise NarrationError("--voice-pacing deve ser v2 ou legado.")
    if pause_ms is not None and not 0 <= pause_ms <= 2000:
        raise NarrationError("--pause-ms deve ficar entre 0 e 2000.")
    provider = _provider_name(provider)
    narrators = config.load_narrators()
    narrator = config.narrator_by_id(narrators, narrator_id or narrators["default_narrator"])
    contract = config.load_motion_contract()
    config_errors = validate_delivery_contract(narrators, contract)
    config_errors.extend(
        validate_pilot_narration(
            BEATS_DATA,
            BEATS_METADATA,
            contract,
            production_stage=config.ACTIVE_EPISODE.production_stage,
        )
    )
    if config_errors:
        raise NarrationError("Configuração de narração inválida: " + "; ".join(config_errors))
    narrator = apply_operational_delivery(narrator, contract)
    selected_beats = _select_beats(beats, voice_pacing)
    if not selected_beats:
        raise NarrationError("Nenhum beat selecionado para síntese.")
    raw_dir = raw_dir or output_path.parent / "raw"
    raw_combined_path = raw_combined_path or raw_dir / "narration_raw.wav"
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timing_path.parent.mkdir(parents=True, exist_ok=True)

    key = region = ""
    if provider == "azure_sdk":
        try:
            key, region = config.azure_credentials(narrator, env_file)
        except config.ConfigError as exc:
            raise NarrationError(str(exc)) from exc
    synthesize = _provider_function(provider)
    sample_rate: int | None = None
    combined = array("h")
    timing_beats: list[dict[str, Any]] = []
    timing_pauses: list[dict[str, Any]] = []
    global_index = 1
    cursor_samples = 0
    warnings: list[str] = []
    provider_quality = "WORD_BOUNDARY_REAL" if provider == "azure_sdk" else "HEURISTIC"
    print(f"NARRATION_ENGINE=src.narration.engine | provider={provider} | voice={narrator['voice']}")

    for index, beat in enumerate(selected_beats, start=1):
        try:
            result = synthesize(beat, narrator=narrator, key=key, region=region)
            current_rate, samples = read_pcm_wav(result.audio_data)
        except (ProviderError, OSError, ValueError) as exc:
            raise NarrationError(f"Falha no beat {beat['beat_id']}: {exc}") from exc
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            raise NarrationError("Taxa de amostragem divergente entre beats.")
        raw_path = raw_dir / f"{beat['beat_id'].lower()}_azure_antonio.wav"
        write_pcm_wav(raw_path, sample_rate, samples)
        offset = cursor_samples / sample_rate
        if provider == "azure_sdk":
            words, speech_start, speech_end = _sdk_words(
                result.boundaries, offset_seconds=offset, global_index=global_index
            )
        else:
            words, speech_start, speech_end = align_words(
                beat["raw_text"], samples, sample_rate,
                offset_seconds=offset, global_index=global_index,
            )
        global_index += len(words)
        beat_duration = len(samples) / sample_rate
        active_speech_duration = speech_end - speech_start
        words_per_sec = round(len(words) / max(0.1, active_speech_duration), 2)
        density_warning = False
        threshold = contract["voice_pacing"]["speech_density"]["warning_threshold_words_per_sec"]
        if words_per_sec > threshold:
            density_warning = True
            warning = f"VOICE_DENSITY_WARNING em {beat['beat_id']}: {words_per_sec} palavras/s (> {threshold})"
            warnings.append(warning)
            print(f"  [ALERTA] {warning}")
        combined.extend(samples)
        cursor_samples += len(samples)
        row = {
            "beat_id": beat["beat_id"],
            "title": beat["title"],
            "text": beat["raw_text"],
            "start": round(offset, 4),
            "speech_start": speech_start,
            "speech_end": speech_end,
            "end": round(cursor_samples / sample_rate, 4),
            "duration_seconds": round(beat_duration, 4),
            "words_count": len(words),
            "words_per_second": words_per_sec,
            "voice_density_warning": density_warning,
            "raw_audio_file": raw_path.relative_to(config.ROOT).as_posix(),
            "raw_audio_sha256": _sha256(raw_path),
            "words": words,
        }
        if result.metadata:
            row["provider_metadata"] = result.metadata
        if result.metadata.get("synthesis_id"):
            row["synthesis_id"] = result.metadata["synthesis_id"]
        timing_beats.append(row)
        print(f"  [{index}/{len(selected_beats)}] {beat['beat_id']} recebido: dur={beat_duration:.2f}s | fala={active_speech_duration:.2f}s | {words_per_sec} pal/s")
        if index < len(selected_beats):
            pause_after_ms = int(pause_ms if pause_ms is not None else beat["pause_after_ms"])
            pause_start = cursor_samples / sample_rate
            pause_samples = round(sample_rate * pause_after_ms / 1000)
            combined.extend(array("h", [0]) * pause_samples)
            cursor_samples += pause_samples
            timing_pauses.append(
                {
                    "after_beat_id": beat["beat_id"],
                    "start": round(pause_start, 4),
                    "end": round(cursor_samples / sample_rate, 4),
                    "duration_ms": pause_after_ms,
                    "source": "motion_contract.voice_pacing.pauses_ms.beat_end",
                }
            )
    if sample_rate is None:
        raise NarrationError("Nenhum áudio recebido.")
    write_pcm_wav(raw_combined_path, sample_rate, combined)
    processed, processing = (array("h", combined), {"implementation": "none", "duration_preserved": True}) if no_processing else process_voice(combined, sample_rate)
    write_pcm_wav(output_path, sample_rate, processed)
    speech_overlap = sum(
        1 for current, following in zip(timing_beats, timing_beats[1:])
        if current["speech_end"] > following["speech_start"]
    )
    timing = {
        "schema_version": "2.0",
        "episode_id": episode_id,
        "scope": (
            BEATS_METADATA["scope"]
            if len(selected_beats) == len(BEATS_DATA)
            else "selected_pilot_beats"
        ),
        "provider": "azure_speech_sdk" if provider == "azure_sdk" else "kokoro_onnx",
        "voice": narrator["voice"],
        "language": narrator.get("language", "pt-BR"),
        "delivery": {"rate": narrator.get("delivery", {}).get("rate", "0%"), "pitch": narrator.get("delivery", {}).get("pitch", "0%"), "volume": "default"},
        "timing_quality": provider_quality,
        "pacing_contract": "CO_MOTION_CONTRACT_V1",
        "anti_atropelamento": {"speech_overlap": speech_overlap, "overlap_allowed": False, "rule_passed": speech_overlap == 0},
        "voice_density_warnings": warnings,
        "raw_audio_file": raw_combined_path.relative_to(config.ROOT).as_posix(),
        "raw_audio_sha256": _sha256(raw_combined_path),
        "audio_file": output_path.relative_to(config.ROOT).as_posix(),
        "audio_sha256": _sha256(output_path),
        "sample_rate": sample_rate,
        "channels": 1,
        "duration_seconds": round(len(processed) / sample_rate, 4),
        "audio_processing": processing,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "beats": timing_beats,
        "pauses": timing_pauses,
    }
    timing_path.write_text(json.dumps(timing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"NARRATION_AUDIO={config.relative_path(output_path)}")
    print(f"NARRATION_TIMING={config.relative_path(timing_path)}")
    print(f"NARRATION_DURATION={timing['duration_seconds']:.4f}s")
    print(f"SPEECH_OVERLAP={speech_overlap}")
    return timing


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Engine único de narração do Capital Oculto.", add_help=add_help)
    parser.add_argument(
        "narration_action", nargs="?",
        choices=("benchmark-voices", "cleanup-humberto", "benchmark-azure-48k"),
    )
    parser.add_argument("--entrada", "--input", type=Path, default=config.DEFAULT_INPUT)
    parser.add_argument("--beats", default=None)
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--narration-plan",
        type=Path,
        default=config.DEFAULT_OUTPUT / "narration_plan.json",
    )
    parser.add_argument("--saida", "--output", type=Path, default=config.DEFAULT_OUTPUT / "narration.wav")
    parser.add_argument("--timing-json", type=Path, default=config.DEFAULT_OUTPUT / "timing.json")
    parser.add_argument("--raw-dir", type=Path, default=config.DEFAULT_OUTPUT / "raw")
    parser.add_argument("--raw-combined", type=Path, default=config.DEFAULT_OUTPUT / "raw" / "narration_raw.wav")
    parser.add_argument(
        "--episodio-id",
        "--episode",
        dest="episode_id",
        default=config.ACTIVE_EPISODE.episode_id,
    )
    parser.add_argument("--narrator", default=None)
    parser.add_argument("--provider", choices=("azure_sdk", "local_kokoro", "azure", "kokoro", "local"), default=None)
    parser.add_argument("--env-file", type=Path, default=config.DEFAULT_ENV_PATH)
    parser.add_argument("--pause-ms", type=int, default=None)
    parser.add_argument("--no-processing", action="store_true")
    parser.add_argument("--voice-pacing", choices=("v2", "legado"), default="v2")
    parser.add_argument("--voices", default=None)
    parser.add_argument("--output-gain-db", type=float, default=-3.0)
    parser.add_argument("--output-gain-variants-db", default=None)
    parser.add_argument("--reuse-official-cache", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--source-raw-dir", type=Path, default=None)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--benchmark-output",
        type=Path,
        default=config.DEFAULT_OUTPUT / "voice_benchmark",
    )
    parser.add_argument(
        "--azure-48k-output",
        type=Path,
        default=config.DEFAULT_OUTPUT / "azure_48k_benchmark",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.narration_action == "benchmark-azure-48k":
        try:
            azure_48k_beats = tuple(
                item.strip()
                for item in (args.beats or ",".join(AZURE_48K_DEFAULT_BEATS)).split(",")
                if item.strip()
            )
            azure_48k_plan = build_azure_48k_plan(
                episode_id=args.episode_id,
                script_path=config.project_path(args.entrada),
                delivery_path=config.ACTIVE_EPISODE.file("narration_delivery.json"),
                benchmark_root=config.project_path(args.azure_48k_output),
                beats=azure_48k_beats,
                run_id=args.run_id,
            )
            if args.dry_run:
                print(json.dumps(azure_48k_dry_run_summary(azure_48k_plan), ensure_ascii=False, indent=2))
                return 0
            narrators = config.load_narrators()
            credential_narrator = config.narrator_by_id(
                narrators, narrators["default_narrator"]
            )
            key, region = config.azure_credentials(
                credential_narrator, config.project_path(args.env_file)
            )
            azure_48k_manifest = run_azure_48k_benchmark(
                plan=azure_48k_plan, key=key, region=region
            )
        except (
            Azure48kBenchmarkError,
            NarrationScriptError,
            DeliveryCueError,
            OfficialNarrationError,
            config.ConfigError,
            ProviderError,
            OSError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        manifest_path = Path(azure_48k_plan["run_dir"]) / "manifest.json"
        print(f"AZURE_48K_MANIFEST={config.relative_path(manifest_path)}")
        print(f"AZURE_CALLS_TOTAL={azure_48k_manifest['azure_calls_total']}")
        return 0
    if args.narration_action == "cleanup-humberto":
        try:
            cleanup_beats = tuple(
                item.strip()
                for item in (args.beats or ",".join(CLEANUP_DEFAULT_BEATS)).split(",")
                if item.strip()
            )
            benchmark_root = config.project_path(args.benchmark_output)
            source_raw_dir = (
                config.project_path(args.source_raw_dir)
                if args.source_raw_dir is not None
                else find_humberto_source(benchmark_root, beats=cleanup_beats)
            )
            if args.dry_run:
                print(json.dumps({
                    "voice": "pt-BR-HumbertoNeural",
                    "beats": list(cleanup_beats),
                    "source_raw_directory": config.relative_path(source_raw_dir),
                    "variants": [
                        "baseline", "gentle_denoise", "gentle_lowpass",
                        "gentle_denoise_lowpass",
                    ],
                    "output_gain_db": -9.0,
                    "azure_calls_total": 0,
                    "official_artifacts_touched": False,
                }, ensure_ascii=False, indent=2))
                return 0
            cleanup_manifest = run_humberto_cleanup(
                source_raw_dir=source_raw_dir,
                benchmark_root=benchmark_root,
                script_path=config.project_path(args.entrada),
                delivery_path=config.ACTIVE_EPISODE.file("narration_delivery.json"),
                episode_id=args.episode_id,
                beats=cleanup_beats,
                run_id=args.run_id,
                ffmpeg=args.ffmpeg,
            )
        except (
            CleanupError,
            VoiceBenchmarkError,
            NarrationScriptError,
            DeliveryCueError,
            OfficialNarrationError,
            config.ConfigError,
            ProviderError,
            OSError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        output = benchmark_root / cleanup_manifest["run_id"] / "humberto_cleanup" / "manifest.json"
        print(f"CLEANUP_MANIFEST={config.relative_path(output)}")
        print("AZURE_CALLS_TOTAL=0")
        return 0
    if args.narration_action == "benchmark-voices":
        try:
            benchmark_beats = tuple(
                item.strip()
                for item in (args.beats or ",".join(BENCHMARK_DEFAULT_BEATS)).split(",")
                if item.strip()
            )
            benchmark_voices = tuple(
                item.strip()
                for item in (args.voices or ",".join(BENCHMARK_DEFAULT_VOICES)).split(",")
                if item.strip()
            )
            gain_variants = (
                tuple(
                    float(item.strip())
                    for item in args.output_gain_variants_db.split(",")
                    if item.strip()
                )
                if args.output_gain_variants_db is not None
                else None
            )
            benchmark_plan = plan_voice_benchmark(
                episode_id=args.episode_id,
                script_path=config.project_path(args.entrada),
                delivery_path=config.ACTIVE_EPISODE.file("narration_delivery.json"),
                benchmark_root=config.project_path(args.benchmark_output),
                beats=benchmark_beats,
                voices=benchmark_voices,
                output_gain_db=args.output_gain_db,
                output_gain_variants_db=gain_variants,
                reuse_official_cache=args.reuse_official_cache,
                official_raw_dir=config.DEFAULT_OUTPUT / "raw",
                run_id=args.run_id,
            )
            if args.dry_run:
                print(json.dumps(dry_run_summary(benchmark_plan), ensure_ascii=False, indent=2))
                return 0
            narrators = config.load_narrators()
            credential_narrator = config.narrator_by_id(
                narrators, narrators["default_narrator"]
            )
            key, region = config.azure_credentials(
                credential_narrator, config.project_path(args.env_file)
            )
            manifest = run_voice_benchmark(
                plan=benchmark_plan,
                key=key,
                region=region,
                official_raw_dir=config.DEFAULT_OUTPUT / "raw",
            )
        except (
            VoiceBenchmarkError,
            NarrationScriptError,
            DeliveryCueError,
            OfficialNarrationError,
            config.ConfigError,
            ProviderError,
            OSError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        print(f"BENCHMARK_MANIFEST={config.relative_path(benchmark_plan['run_dir'] / 'manifest.json')}")
        print(f"AZURE_CALLS_TOTAL={manifest['azure_calls_total']}")
        return 0
    if args.official:
        if args.beats is not None:
            print(
                "ERRO: --official does not allow partial --beats selection.",
                file=sys.stderr,
            )
            return 2
        try:
            plan = build_official_narration_plan(
                input_path=config.project_path(args.entrada),
                output_path=config.project_path(args.narration_plan),
                episode_id=args.episode_id,
                narrator_id=args.narrator,
            )
            if not args.dry_run:
                if args.provider is not None and _provider_name(args.provider) != "azure_sdk":
                    raise NarrationError("Official narration requires provider azure_sdk; no fallback is allowed.")
                if args.pause_ms is not None or args.no_processing or args.voice_pacing != "v2":
                    raise NarrationError("Official narration uses canonical pacing and processing settings.")
                narrators = config.load_narrators()
                narrator = config.narrator_by_id(narrators, args.narrator or narrators["default_narrator"])
                contract = config.load_motion_contract()
                errors = validate_delivery_contract(narrators, contract)
                if errors:
                    raise NarrationError("Configuração de narração inválida: " + "; ".join(errors))
                narrator = apply_operational_delivery(narrator, contract)
                key, region = config.azure_credentials(narrator, config.project_path(args.env_file))
                preserve_official_rollback(
                    output_path=config.project_path(args.saida),
                    timing_path=config.project_path(args.timing_json),
                    raw_dir=config.project_path(args.raw_dir),
                )
                timing = run_official_narration(
                    plan=plan, narrator=narrator, contract=contract,
                    output_path=config.project_path(args.saida),
                    timing_path=config.project_path(args.timing_json),
                    raw_dir=config.project_path(args.raw_dir),
                    raw_combined_path=config.project_path(args.raw_combined),
                    synthesize=azure_sdk.synthesize, key=key, region=region,
                    benchmark_cache_root=config.DEFAULT_OUTPUT / "azure_48k_benchmark",
                )
        except (
            NarrationError,
            NarrationScriptError,
            DeliveryCueError,
            OfficialNarrationError,
            config.ConfigError,
            OSError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        print(f"NARRATION_PLAN={config.relative_path(config.project_path(args.narration_plan))}")
        print(f"OFFICIAL_BEATS={plan['beat_count']}")
        print(f"OFFICIAL_WORDS={plan['word_count']}")
        if not args.dry_run:
            print(f"NARRATION_AUDIO={timing['audio_file']}")
            print(f"NARRATION_TIMING={config.relative_path(config.project_path(args.timing_json))}")
        return 0
    if args.dry_run:
        print("ERRO: --dry-run requires --official.", file=sys.stderr)
        return 2
    provider = args.provider
    if provider is None:
        env = config.load_env(config.project_path(args.env_file))
        provider = env.get("TTS_PROVIDER", "azure_sdk")
    try:
        run_narration(
            input_path=config.project_path(args.entrada),
            beats=args.beats,
            output_path=config.project_path(args.saida),
            timing_path=config.project_path(args.timing_json),
            raw_dir=config.project_path(args.raw_dir),
            raw_combined_path=config.project_path(args.raw_combined),
            episode_id=args.episode_id,
            narrator_id=args.narrator,
            provider=provider,
            env_file=config.project_path(args.env_file),
            pause_ms=args.pause_ms,
            no_processing=args.no_processing,
            voice_pacing=args.voice_pacing,
        )
    except (NarrationError, config.ConfigError, ProviderError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

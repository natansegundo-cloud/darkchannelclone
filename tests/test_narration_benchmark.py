from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import unittest
import wave
from array import array
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import main as project_main
from src.narration import config
from src.narration.benchmark import (
    apply_output_gain,
    dry_run_summary,
    plan_voice_benchmark,
    run_voice_benchmark,
)
from src.narration.providers import ProviderResult
from src.narration.providers.pacing import normalize_word, read_pcm_wav, write_pcm_wav


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "episodios" / "CO-001" / "roteiro_narracao.md"
DELIVERY = ROOT / "episodios" / "CO-001" / "narration_delivery.json"


def wav_bytes(amplitude: int = 4000) -> bytes:
    target = io.BytesIO()
    samples = array("h", [amplitude, -amplitude] * 1200)
    with wave.open(target, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(samples.tobytes())
    return target.getvalue()


def result_for(beat: dict, voice: str = "candidate") -> ProviderResult:
    boundaries = []
    for index, token in enumerate(re.findall(r"\S+", beat["raw_text"])):
        normalized = normalize_word(token)
        if normalized:
            boundaries.append({
                "text": token,
                "normalized": normalized,
                "audio_offset_ms": 1.0 + index,
                "duration_ms": 0.8,
                "boundary_type": "Word",
                "text_offset": index,
                "word_length": len(token),
            })
    return ProviderResult(
        audio_data=wav_bytes(),
        sample_rate=24_000,
        boundaries=boundaries,
        metadata={
            "timing_quality": "WORD_BOUNDARY_REAL",
            "synthesis_id": f"synth-{voice}-{beat['beat_id']}",
        },
    )


def write_official_cache(raw_dir: Path, beat: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    wav_path = raw_dir / f"{beat['beat_id']}.wav"
    rate, samples = read_pcm_wav(wav_bytes())
    write_pcm_wav(wav_path, rate, samples)
    provider_result = result_for(beat, "antonio")
    metadata = {
        "episode_id": "CO-001",
        "provider": "azure_sdk",
        "voice": "pt-BR-AntonioNeural",
        "rate": "-7%",
        "pitch": "0%",
        "beat_id": beat["beat_id"],
        "source_hash": beat["source_hash"],
        "synthesis_input_hash": beat["synthesis_input_hash"],
        "raw_text": beat["raw_text"],
        "sample_rate": rate,
        "audio_sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "timing_quality": "WORD_BOUNDARY_REAL",
        "synthesis_id": provider_result.metadata["synthesis_id"],
        "word_boundaries": provider_result.boundaries,
        "completed": True,
    }
    (raw_dir / f"{beat['beat_id']}.json").write_text(json.dumps(metadata), encoding="utf-8")


class NarrationBenchmarkTest(unittest.TestCase):
    def make_plan(self, root: Path, raw_dir: Path, *, reuse: bool = True, run_id: str = "test-run"):
        return plan_voice_benchmark(
            episode_id="CO-001",
            script_path=SCRIPT,
            delivery_path=DELIVERY,
            benchmark_root=root / "voice_benchmark",
            beats=("B001", "B005"),
            voices=("pt-BR-AntonioNeural", "pt-BR-FabioNeural"),
            output_gain_db=-3.0,
            reuse_official_cache=reuse,
            official_raw_dir=raw_dir,
            run_id=run_id,
        )

    def test_valid_antonio_cache_is_reused_and_word_boundaries_keep_synthesis(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            raw_dir = root / "official-raw"
            seed = self.make_plan(root, raw_dir, reuse=False, run_id="seed")
            for entry in seed["voice_plans"][0]["beats"]:
                write_official_cache(raw_dir, entry["beat"])
            plan = self.make_plan(root, raw_dir)
            synth = mock.Mock(side_effect=lambda beat, narrator, **_: result_for(beat, narrator["voice"]))
            manifest = run_voice_benchmark(
                plan=plan, key="mock", region="mock", official_raw_dir=raw_dir, synthesize=synth
            )
            self.assertEqual(synth.call_count, 2)
            self.assertTrue(all(
                item["cache_source"] == "official_cache"
                for item in manifest["voice_results"]["pt-BR-AntonioNeural"]["beats"].values()
            ))
            self.assertEqual(manifest["azure_calls_total"], 2)
            for voice in manifest["voice_results"].values():
                for beat in voice["beats"].values():
                    self.assertTrue(beat["same_synthesis_audio_timing"])
                    self.assertTrue(beat["synthesis_id"])
            self.assertTrue((plan["run_dir"] / "antonio" / "comparison.wav").is_file())
            self.assertTrue((plan["run_dir"] / "fabio" / "comparison.wav").is_file())

    def test_incompatible_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            raw_dir = root / "official-raw"
            seed = self.make_plan(root, raw_dir, reuse=False, run_id="seed")
            beat = seed["voice_plans"][0]["beats"][0]["beat"]
            write_official_cache(raw_dir, beat)
            meta_path = raw_dir / "B001.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["rate"] = "-6%"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            plan = plan_voice_benchmark(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=root / "voice_benchmark", beats=("B001",),
                voices=("pt-BR-AntonioNeural",), output_gain_db=-3,
                reuse_official_cache=True, official_raw_dir=raw_dir, run_id="incompatible",
            )
            self.assertEqual(plan["azure_calls_total"], 1)
            self.assertEqual(plan["voice_plans"][0]["beats"][0]["cache_source"], "azure")

    def test_dry_run_has_zero_calls_and_writes_nothing(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            target = Path(name) / "benchmark"
            output = io.StringIO()
            with mock.patch("src.narration.benchmark.azure_sdk.synthesize") as synth, redirect_stdout(output):
                exit_code = project_main.main([
                    "narracao", "benchmark-voices", "--beats", "B001,B005",
                    "--voices", "pt-BR-AntonioNeural,pt-BR-FabioNeural",
                    "--benchmark-output", str(target), "--run-id", "dry-test",
                    "--reuse-official-cache", "--dry-run",
                ])
            self.assertEqual(exit_code, 0)
            synth.assert_not_called()
            self.assertFalse(target.exists())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["azure_calls_total"], 0)
            self.assertIn("pt-BR-FabioNeural", payload["azure_calls_by_voice"])

    def test_output_gain_reduces_amplitude_without_duration_or_clipping(self):
        original = array("h", [30000, -30000, 15000, -15000])
        gained = apply_output_gain(original, -3.0)
        self.assertEqual(len(original), len(gained))
        self.assertLess(max(abs(value) for value in gained), max(abs(value) for value in original))
        self.assertLessEqual(max(abs(value) for value in gained), 32767)
        ratio = max(abs(value) for value in gained) / max(abs(value) for value in original)
        self.assertAlmostEqual(ratio, 10 ** (-3 / 20), places=4)

    def test_benchmark_does_not_touch_official_artifacts(self):
        official_paths = [
            config.DEFAULT_OUTPUT / "narration.wav",
            config.DEFAULT_OUTPUT / "timing.json",
            config.DEFAULT_OUTPUT / "raw" / "B001.wav",
            config.DEFAULT_OUTPUT / "raw" / "B001.json",
        ]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths}
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            plan = plan_voice_benchmark(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=root / "voice_benchmark", beats=("B001",),
                voices=("pt-BR-AntonioNeural",), output_gain_db=-3,
                reuse_official_cache=True, official_raw_dir=config.DEFAULT_OUTPUT / "raw",
                run_id="isolated",
            )
            run_voice_benchmark(
                plan=plan, key="mock", region="mock",
                official_raw_dir=config.DEFAULT_OUTPUT / "raw",
                synthesize=mock.Mock(side_effect=lambda beat, narrator, **_: result_for(beat, narrator["voice"])),
            )
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths}
        self.assertEqual(before, after)

    def test_dry_run_summary_reports_expected_real_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            plan = self.make_plan(root, root / "missing", reuse=True)
            summary = dry_run_summary(plan)
            self.assertEqual(summary["azure_calls_total"], 0)
            self.assertEqual(summary["expected_real_azure_calls_total"], 4)

    def test_humberto_single_synthesis_feeds_three_gain_variants_and_cache(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            kwargs = dict(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=root / "voice_benchmark", beats=("B001", "B005"),
                voices=("pt-BR-HumbertoNeural",), output_gain_variants_db=(-7, -8, -9),
                official_raw_dir=root / "official-raw",
            )
            plan = plan_voice_benchmark(**kwargs, run_id="humberto-first")
            synth = mock.Mock(
                side_effect=lambda beat, narrator, **_: result_for(beat, narrator["voice"])
            )
            manifest = run_voice_benchmark(
                plan=plan, key="mock", region="mock", synthesize=synth,
                official_raw_dir=root / "official-raw",
            )
            self.assertEqual(synth.call_count, 2)
            voice = manifest["voice_results"]["pt-BR-HumbertoNeural"]
            variants = voice["variants"]
            self.assertEqual(set(variants), {"gain_minus_7db", "gain_minus_8db", "gain_minus_9db"})
            for beat_id in ("B001", "B005"):
                rows = [variants[name]["beats"][beat_id] for name in variants]
                self.assertEqual(len({row["raw_audio_sha256"] for row in rows}), 1)
                self.assertEqual(len({row["synthesis_id"] for row in rows}), 1)
                self.assertEqual(len({row["duration"] for row in rows}), 1)
                self.assertTrue(all(row["same_synthesis_audio_timing"] for row in rows))
                before = rows[0]["sample_peak_dbfs_before_output_gain"]
                by_gain = {row["output_gain_db"]: row for row in rows}
                self.assertAlmostEqual(by_gain[-8]["sample_peak_dbfs"], before - 8, delta=.01)
                self.assertAlmostEqual(
                    by_gain[-9]["sample_peak_dbfs"] - by_gain[-7]["sample_peak_dbfs"],
                    -2,
                    delta=.01,
                )
                self.assertTrue(all(row["clipping_samples_after_output_gain"] == 0 for row in rows))
            durations = [variant["comparison_duration"] for variant in variants.values()]
            self.assertEqual(len(set(durations)), 1)

            cached_plan = plan_voice_benchmark(**kwargs, run_id="humberto-cached")
            self.assertEqual(cached_plan["azure_calls_total"], 0)
            cached_synth = mock.Mock()
            cached_manifest = run_voice_benchmark(
                plan=cached_plan, key="mock", region="mock", synthesize=cached_synth,
                official_raw_dir=root / "official-raw",
            )
            cached_synth.assert_not_called()
            self.assertEqual(cached_manifest["azure_calls_total"], 0)

    def test_upstream_clipping_is_detected_before_output_gain(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            plan = plan_voice_benchmark(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=root / "voice_benchmark", beats=("B001",),
                voices=("pt-BR-HumbertoNeural",), output_gain_variants_db=(-7, -8, -9),
                official_raw_dir=root / "official-raw", run_id="clipping",
            )

            def clipping_processor(samples, _rate):
                return array("h", [32767] * len(samples)), {
                    "duration_preserved": True,
                    "compressor_max_gain_reduction_db": 13.0,
                    "limiter_peak_reduction_db": 0.0,
                }

            manifest = run_voice_benchmark(
                plan=plan, key="mock", region="mock",
                synthesize=mock.Mock(side_effect=lambda beat, narrator, **_: result_for(beat, narrator["voice"])),
                processor=clipping_processor,
                official_raw_dir=root / "official-raw",
            )
            self.assertTrue(manifest["upstream_clipping_suspected"])
            self.assertTrue(manifest["overprocessing_suspected"])
            rows = manifest["voice_results"]["pt-BR-HumbertoNeural"]["variants"]
            self.assertTrue(all(
                variant["beats"]["B001"]["clipping_samples_before_output_gain"] > 0
                for variant in rows.values()
            ))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

from src.narration import config
from src.narration.azure_48k_benchmark import (
    OUTPUT_SAMPLE_RATE,
    VOICES,
    build_plan,
    dry_run_summary,
    run_benchmark,
)
from src.narration.providers import ProviderResult
from src.narration.providers.pacing import normalize_word, read_pcm_wav


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "episodios" / "CO-001" / "roteiro_narracao.md"
DELIVERY = ROOT / "episodios" / "CO-001" / "narration_delivery.json"


def wav_48k() -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(OUTPUT_SAMPLE_RATE)
        handle.writeframes(array("h", [3000, -3000] * 2400).tobytes())
    return target.getvalue()


def result_for(beat: dict, voice: str) -> ProviderResult:
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
        audio_data=wav_48k(),
        sample_rate=OUTPUT_SAMPLE_RATE,
        boundaries=boundaries,
        metadata={
            "timing_quality": "WORD_BOUNDARY_REAL",
            "synthesis_id": f"synth-{voice}-{beat['beat_id']}",
        },
    )


class Azure48kBenchmarkTest(unittest.TestCase):
    def test_raw_48k_delivery_timing_cache_and_official_isolation(self):
        official_paths = [
            config.DEFAULT_OUTPUT / "narration.wav",
            config.DEFAULT_OUTPUT / "timing.json",
            config.DEFAULT_OUTPUT / "raw" / "B001.wav",
            config.DEFAULT_OUTPUT / "raw" / "B001.json",
        ]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths}
        calls = []
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            benchmark_root = root / "azure_48k_benchmark"
            plan = build_plan(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=benchmark_root, run_id="first",
            )
            self.assertEqual(plan["expected_azure_calls"], 9)
            b005 = plan["voices"][0]["beats"][1]["beat"]
            self.assertEqual(
                [cue["milliseconds"] for cue in b005["delivery_cues"]], [450, 500]
            )

            def synthesize(beat, *, narrator, output_sample_rate, **_kwargs):
                self.assertEqual(output_sample_rate, 48_000)
                calls.append((narrator["voice"], beat["beat_id"], beat["raw_text"]))
                return result_for(beat, narrator["voice"])

            with mock.patch("src.narration.providers.pacing.process_voice") as processor:
                manifest = run_benchmark(
                    plan=plan, key="mock", region="mock", synthesize=synthesize
                )
            processor.assert_not_called()
            self.assertEqual(len(calls), 9)
            self.assertEqual(manifest["azure_calls_total"], 9)
            self.assertEqual(manifest["raw_processing"], "NONE")
            for voice, slug in VOICES:
                metadata_path = plan["run_dir"] / slug / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["raw_processing"], "NONE")
                rate, _samples = read_pcm_wav((plan["run_dir"] / slug / "comparison.wav").read_bytes())
                self.assertEqual(rate, 48_000)
                for beat_id, row in metadata["beats"].items():
                    raw_path = plan["run_dir"] / slug / f"{beat_id}.wav"
                    self.assertEqual(raw_path.read_bytes(), wav_48k())
                    raw_rate, _raw_samples = read_pcm_wav(raw_path.read_bytes())
                    self.assertEqual(raw_rate, 48_000)
                    self.assertEqual(row["bit_depth"], 16)
                    self.assertEqual(row["timing_quality"], "WORD_BOUNDARY_REAL")
                    self.assertTrue(row["same_synthesis_audio_timing"])
                    self.assertEqual(row["raw_processing"], "NONE")
                    self.assertTrue(row["synthesis_id"].startswith(f"synth-{voice}"))
                    if beat_id == "B005":
                        self.assertEqual(
                            [cue["milliseconds"] for cue in row["delivery_cues"]],
                            [450, 500],
                        )

            cached_plan = build_plan(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=benchmark_root, run_id="cached",
            )
            self.assertEqual(cached_plan["expected_azure_calls"], 0)
            cached_synth = mock.Mock()
            cached_manifest = run_benchmark(
                plan=cached_plan, key="mock", region="mock", synthesize=cached_synth
            )
            cached_synth.assert_not_called()
            self.assertEqual(cached_manifest["azure_calls_total"], 0)

        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths}
        self.assertEqual(before, after)

    def test_dry_run_has_zero_real_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            plan = build_plan(
                episode_id="CO-001", script_path=SCRIPT, delivery_path=DELIVERY,
                benchmark_root=root / "azure_48k_benchmark", run_id="dry",
            )
            summary = dry_run_summary(plan)
            self.assertEqual(summary["azure_calls_total"], 0)
            self.assertEqual(summary["expected_azure_calls"], 9)
            self.assertEqual(summary["raw_processing"], "NONE")
            self.assertFalse((root / "azure_48k_benchmark").exists())


if __name__ == "__main__":
    unittest.main()

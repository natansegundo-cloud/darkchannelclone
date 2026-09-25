from __future__ import annotations

import io
import json
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

import main as project_main
from src.narration import engine
from src.narration.providers import ProviderError, ProviderResult


ROOT = Path(__file__).resolve().parents[1]


def wav_fixture() -> bytes:
    target = io.BytesIO()
    samples = array("h", [0] * 24_000)
    with wave.open(target, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(samples.tobytes())
    return target.getvalue()


def sdk_result() -> ProviderResult:
    return ProviderResult(
        audio_data=wav_fixture(),
        sample_rate=24_000,
        boundaries=[
            {
                "text": "Você",
                "normalized": "voce",
                "audio_offset_ms": 50.0,
                "duration_ms": 300.0,
                "boundary_type": "Word",
                "text_offset": 0,
                "word_length": 4,
            }
        ],
        metadata={
            "timing_quality": "WORD_BOUNDARY_REAL",
            "sdk_package": "azure-cognitiveservices-speech==1.51.2",
            "synthesis_id": "test-synthesis-id",
        },
    )


class NarrationEngineSmokeTest(unittest.TestCase):
    def test_main_defaults_to_sdk_and_preserves_synthesis_link(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".tmp-narration-smoke-", dir=ROOT) as temporary:
            target = Path(temporary)
            output = target / "b001.wav"
            timing_path = target / "b001.json"
            with (
                mock.patch.object(engine.config, "load_env", return_value={}),
                mock.patch.object(engine.config, "azure_credentials", return_value=("key", "region")),
                mock.patch.object(engine.azure_sdk, "synthesize", return_value=sdk_result()) as sdk,
                mock.patch.object(engine.local_kokoro, "synthesize") as local,
            ):
                exit_code = project_main.main([
                    "narracao",
                    "--beats", "B001",
                    "--saida", str(output),
                    "--timing-json", str(timing_path),
                    "--raw-dir", str(target / "raw"),
                    "--raw-combined", str(target / "raw" / "combined.wav"),
                    "--no-processing",
                ])

            self.assertEqual(exit_code, 0)
            sdk.assert_called_once()
            local.assert_not_called()
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            self.assertEqual(timing["provider"], "azure_speech_sdk")
            self.assertEqual(timing["timing_quality"], "WORD_BOUNDARY_REAL")
            self.assertEqual(timing["beats"][0]["synthesis_id"], "test-synthesis-id")
            self.assertEqual(timing["beats"][0]["provider_metadata"]["synthesis_id"], "test-synthesis-id")
            self.assertTrue(output.is_file())

    def test_sdk_failure_does_not_fallback_silently(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".tmp-narration-failure-", dir=ROOT) as temporary:
            target = Path(temporary)
            with (
                mock.patch.object(engine.config, "azure_credentials", return_value=("key", "region")),
                mock.patch.object(engine.azure_sdk, "synthesize", side_effect=ProviderError("sdk failure")),
                mock.patch.object(engine.local_kokoro, "synthesize") as local,
            ):
                with self.assertRaises(engine.NarrationError):
                    engine.run_narration(
                        input_path=ROOT / "episodios" / "CO-001" / "roteiro_narracao.md",
                        beats="B001",
                        output_path=target / "failed.wav",
                        timing_path=target / "failed.json",
                        raw_dir=target / "raw",
                        raw_combined_path=target / "raw" / "combined.wav",
                        provider="azure_sdk",
                        no_processing=True,
                    )
            local.assert_not_called()


class CanonicalConfigTest(unittest.TestCase):
    def test_configs_and_preserved_timings_are_canonical(self) -> None:
        project = json.loads((ROOT / "config" / "project.json").read_text(encoding="utf-8"))
        narrators = json.loads((ROOT / "config" / "narrators.json").read_text(encoding="utf-8"))
        motion = json.loads((ROOT / "config" / "motion_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(project["canonical_provider"], "azure_sdk")
        self.assertEqual(project["timing_quality"], "WORD_BOUNDARY_REAL")
        self.assertEqual(narrators["narrators"][0]["provider"], "azure_sdk")
        self.assertEqual(motion["voice_pacing"]["provider"], "azure_sdk")

        for name in ("b001-b006.json", "b007-b009.json", "b010-b013.json"):
            timing = json.loads((ROOT / "episodios" / "CO-001" / "timing" / name).read_text(encoding="utf-8-sig"))
            self.assertEqual(timing["provider"], "azure_speech_sdk")
            self.assertEqual(timing["timing_quality"], "WORD_BOUNDARY_REAL")
            self.assertTrue(all(beat.get("synthesis_id") for beat in timing["beats"]))


if __name__ == "__main__":
    unittest.main()

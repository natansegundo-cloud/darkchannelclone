from __future__ import annotations

import io
import json
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

from src.narration import config
from src.narration.official import OfficialNarrationError, run_official_narration
from src.narration.providers import ProviderResult
from src.narration.delivery import apply_delivery_cues
from src.narration.providers.pacing import normalize_word
import re

ROOT = Path(__file__).resolve().parents[1]


def wav() -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(48_000)
        out.writeframes(array("h", [1000] * 4800).tobytes())
    return target.getvalue()


def result(beat: dict) -> ProviderResult:
    boundaries = []
    for index, token in enumerate(re.findall(r"\S+", beat["raw_text"])):
        boundaries.append({"text": token, "normalized": normalize_word(token), "audio_offset_ms": 10.0 + index * 20,
            "duration_ms": 10.0, "boundary_type": "Word", "text_offset": None, "word_length": len(token)})
    return ProviderResult(audio_data=wav(), sample_rate=48_000, boundaries=boundaries,
        metadata={"timing_quality": "WORD_BOUNDARY_REAL", "synthesis_id": f"synth-{beat['beat_id']}"})


def plan(texts=("um", "dois", "tres"), cues=None) -> dict:
    beats = [{"beat_id": f"B{i:03d}", "title": str(i), "raw_text": text,
              "source_hash": __import__("hashlib").sha256(text.encode()).hexdigest(), "word_count": 1}
             for i, text in enumerate(texts, 1)]
    beats = apply_delivery_cues(beats, cues or {}, provider="azure_sdk", voice="pt-BR-HumbertoNeural", rate="-7%", pitch="0%", output_format="riff-48khz-16bit-mono-pcm")
    return {"episode_id": "CO-001", "voice": "pt-BR-HumbertoNeural", "rate": "-7%", "pitch": "0%",
            "output_format": "riff-48khz-16bit-mono-pcm", "sample_rate": 48000, "bit_depth": 16, "channels": 1,
            "source_file": "episodios/CO-001/roteiro_narracao.md", "source_sha256": "script", "beats": beats}


class OfficialNarrationTest(unittest.TestCase):
    def execute(self, root: Path, current: dict, synth, processor=None):
        kwargs = dict(plan=current, narrator={"voice": "pt-BR-HumbertoNeural", "delivery": {"rate": "-7%", "pitch": "0%"}},
            contract=config.load_motion_contract(), output_path=root / "narration.wav", timing_path=root / "timing.json",
            raw_dir=root / "raw", raw_combined_path=root / "raw" / "narration_raw.wav",
            synthesize=synth, key="mock", region="mock")
        if processor is not None: kwargs["processor"] = processor
        return run_official_narration(**kwargs)

    def test_resume_and_only_changed_beat_is_synthesized(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name); synth = mock.Mock(side_effect=lambda beat, **_: result(beat))
            timing = self.execute(root, plan(), synth)
            self.assertEqual(synth.call_count, 3)
            self.assertEqual(timing["timing_quality"], "WORD_BOUNDARY_REAL")
            self.assertTrue(timing["same_synthesis_audio_timing"])
            self.assertEqual(timing["sample_rate"], 48_000)
            self.assertEqual(timing["audio_processing"]["implementation"], "linear_headroom_only")
            self.assertFalse(timing["audio_processing"]["heavy_processing"])
            self.assertEqual(timing["audio_processing"]["clipping_samples"], 0)
            synth.reset_mock(); self.execute(root, plan(), synth); synth.assert_not_called()
            changed = plan(("um", "DOIS ALTERADO", "tres"))
            self.execute(root, changed, synth)
            self.assertEqual(synth.call_count, 1)
            self.assertEqual(synth.call_args.args[0]["beat_id"], "B002")

    def test_delivery_change_invalidates_only_that_beat(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            synth = mock.Mock(side_effect=lambda beat, **_: result(beat))
            self.execute(root, plan(), synth)
            synth.reset_mock()
            changed = plan(cues={"B002": [{"text": "dois", "milliseconds": 450}]})
            self.execute(root, changed, synth)
            self.assertEqual([call.args[0]["beat_id"] for call in synth.call_args_list], ["B002"])

    def test_corrupt_hash_and_missing_wav_invalidate_only_their_beats(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name); synth = mock.Mock(side_effect=lambda beat, **_: result(beat))
            self.execute(root, plan(), synth); synth.reset_mock()
            meta_path = root / "raw" / "B001.json"
            meta = json.loads(meta_path.read_text()); meta["audio_sha256"] = "bad"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            (root / "raw" / "B003.wav").unlink()
            self.execute(root, plan(), synth)
            self.assertEqual([call.args[0]["beat_id"] for call in synth.call_args_list], ["B001", "B003"])
            synth.reset_mock()
            heuristic = json.loads((root / "raw" / "B002.json").read_text())
            heuristic["timing_quality"] = "HEURISTIC"
            (root / "raw" / "B002.json").write_text(json.dumps(heuristic), encoding="utf-8")
            self.execute(root, plan(), synth)
            self.assertEqual([call.args[0]["beat_id"] for call in synth.call_args_list], ["B002"])

    def test_mid_failure_resumes_without_fallback_and_processing_must_preserve_duration(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            failing = mock.Mock(side_effect=lambda beat, **_: (_ for _ in ()).throw(RuntimeError("stop")) if beat["beat_id"] == "B002" else result(beat))
            with self.assertRaises(OfficialNarrationError): self.execute(root, plan(), failing)
            self.assertTrue((root / "raw" / "B001.json").is_file())
            self.assertFalse((root / "timing.json").exists())
            resumed = mock.Mock(side_effect=lambda beat, **_: result(beat))
            self.execute(root, plan(), resumed)
            self.assertEqual([call.args[0]["beat_id"] for call in resumed.call_args_list], ["B002", "B003"])
            bad = lambda samples, rate: (array("h", samples[:-1]), {"duration_preserved": True})
            with self.assertRaises(OfficialNarrationError): self.execute(root, plan(), resumed, bad)


if __name__ == "__main__": unittest.main()

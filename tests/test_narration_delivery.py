from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from src.narration import config
from src.narration.delivery import DeliveryCueError, apply_delivery_cues, load_delivery_cues, ssml_body
from src.narration.engine import build_official_narration_plan
from src.narration.script_parser import parse_narration_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "episodios" / "CO-001" / "roteiro_narracao.md"
DELIVERY = ROOT / "episodios" / "CO-001" / "narration_delivery.json"


class NarrationDeliveryTest(unittest.TestCase):
    def test_empty_prosody_preserves_previous_ssml_and_hash(self):
        beat = {"beat_id": "B001", "raw_text": "um & dois"}
        legacy = apply_delivery_cues(
            [beat], {}, provider="azure_sdk", voice="voice", rate="-7%", pitch="0%"
        )[0]
        explicit = apply_delivery_cues(
            [beat], {"B001": {"pause_after": [], "prosody": []}},
            provider="azure_sdk", voice="voice", rate="-7%", pitch="0%"
        )[0]
        self.assertEqual(ssml_body(explicit), "um &amp; dois")
        self.assertEqual(legacy["synthesis_input_hash"], explicit["synthesis_input_hash"])

    def test_each_advanced_prosody_control_compiles_valid_xml(self):
        controls = {
            "rate": "-5%",
            "pitch": "-2%",
            "volume": "-1dB",
            "contour": "(0%,-2%) (50%,+3%) (100%,-3%)",
        }
        for field, value in controls.items():
            with self.subTest(field=field):
                prepared = apply_delivery_cues(
                    [{"beat_id": "B001", "raw_text": "antes trecho depois"}],
                    {"B001": {"prosody": [{"text": "trecho", field: value}]}},
                    provider="azure_sdk", voice="voice", rate="-7%", pitch="0%",
                )[0]
                body = ssml_body(prepared)
                root = ET.fromstring(f"<root>{body}</root>")
                node = root.find("prosody")
                self.assertIsNotNone(node)
                self.assertEqual(node.attrib[field], value)
                self.assertEqual("".join(root.itertext()), prepared["raw_text"])

    def test_combined_prosody_and_pause_compile_in_one_pass(self):
        prepared = apply_delivery_cues(
            [{"beat_id": "B001", "raw_text": "antes trecho depois"}],
            {"B001": {
                "pause_after": [{"text": "trecho", "milliseconds": 450}],
                "prosody": [{"text": "trecho", "rate": "-5%", "pitch": "+1%"}],
            }},
            provider="azure_sdk", voice="voice", rate="-7%", pitch="0%",
        )[0]
        body = ssml_body(prepared)
        self.assertIn('</prosody><break time="450ms"/>', body)
        self.assertEqual("".join(ET.fromstring(f"<root>{body}</root>").itertext()), prepared["raw_text"])

    def test_prosody_missing_ambiguous_and_overlap_fail_closed(self):
        beat = {"beat_id": "B001", "raw_text": "eco no meio eco final"}
        invalid = [
            {"B001": {"prosody": [{"text": "ausente", "rate": "-5%"}]}},
            {"B001": {"prosody": [{"text": "eco", "pitch": "-2%"}]}},
            {"B001": {"prosody": [
                {"text": "no meio", "rate": "-5%"},
                {"text": "meio eco final", "volume": "-1dB"},
            ]}},
        ]
        for cues in invalid:
            with self.subTest(cues=cues), self.assertRaises(DeliveryCueError):
                apply_delivery_cues(
                    [beat], cues, provider="azure_sdk", voice="voice", rate="-7%", pitch="0%"
                )

    def test_b005_cues_and_raw_text_are_exact(self):
        before = SCRIPT.read_bytes()
        beats = parse_narration_script(SCRIPT)
        cues = load_delivery_cues(DELIVERY, beats)
        b005 = next(beat for beat in beats if beat["beat_id"] == "B005")
        self.assertEqual([cue["milliseconds"] for cue in cues["B005"]["pause_after"]], [450, 500])
        prepared = apply_delivery_cues([b005], cues, provider="azure_sdk", voice="pt-BR-AntonioNeural", rate="-7%", pitch="0%")
        body = ssml_body(prepared[0])
        self.assertEqual(body.count("<break "), 2)
        self.assertIn('<break time="450ms"/>', body)
        self.assertIn('<break time="500ms"/>', body)
        self.assertEqual(before, SCRIPT.read_bytes())

    def test_missing_ambiguous_and_invalid_cues_fail_closed(self):
        beats = [{"beat_id": "B001", "raw_text": "eco meio eco"}]
        cases = [
            {"text": "ausente", "milliseconds": 100},
            {"text": "eco", "milliseconds": 100},
            {"text": "meio", "milliseconds": 0},
        ]
        for cue in cases:
            with self.subTest(cue=cue), tempfile.TemporaryDirectory(dir=ROOT) as name:
                path = Path(name) / "delivery.json"
                path.write_text(json.dumps({"schema_version": "1.0", "beats": {"B001": {"pause_after": [cue]}}}), encoding="utf-8")
                with self.assertRaises(DeliveryCueError):
                    load_delivery_cues(path, beats)

    def test_overlapping_occurrences_are_ambiguous(self):
        beats = [{"beat_id": "B001", "raw_text": "aaaa"}]
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            path = Path(name) / "delivery.json"
            path.write_text(json.dumps({"schema_version": "1.0", "beats": {
                "B001": {"pause_after": [{"text": "aa", "milliseconds": 100}]}
            }}), encoding="utf-8")
            with self.assertRaises(DeliveryCueError):
                load_delivery_cues(path, beats)

    def test_delivery_changes_only_synthesis_hash(self):
        beat = {"beat_id": "B001", "raw_text": "um texto", "source_hash": hashlib.sha256(b"um texto").hexdigest()}
        plain = apply_delivery_cues([beat], {}, provider="azure_sdk", voice="voice", rate="-7%", pitch="0%")[0]
        cued = apply_delivery_cues([beat], {"B001": [{"text": "um", "milliseconds": 450}]}, provider="azure_sdk", voice="voice", rate="-7%", pitch="0%")[0]
        self.assertEqual(plain["source_hash"], cued["source_hash"])
        self.assertNotEqual(plain["synthesis_input_hash"], cued["synthesis_input_hash"])

    def test_output_format_is_part_of_synthesis_hash(self):
        beat = {"beat_id": "B001", "raw_text": "um texto"}
        pcm24 = apply_delivery_cues(
            [beat], {}, provider="azure_sdk", voice="pt-BR-HumbertoNeural",
            rate="-7%", pitch="0%", output_format="riff-24khz-16bit-mono-pcm",
        )[0]
        pcm48 = apply_delivery_cues(
            [beat], {}, provider="azure_sdk", voice="pt-BR-HumbertoNeural",
            rate="-7%", pitch="0%", output_format="riff-48khz-16bit-mono-pcm",
        )[0]
        self.assertNotEqual(pcm24["synthesis_input_hash"], pcm48["synthesis_input_hash"])

    def test_official_plan_loads_episode_delivery(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            plan = build_official_narration_plan(input_path=SCRIPT, output_path=Path(name) / "plan.json", episode_id="CO-001")
        b005 = next(beat for beat in plan["beats"] if beat["beat_id"] == "B005")
        self.assertEqual([cue["milliseconds"] for cue in b005["delivery_cues"]], [450, 500])
        self.assertTrue(b005["synthesis_input_hash"])


if __name__ == "__main__":
    unittest.main()

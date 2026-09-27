from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as project_main
from src.narration import config, engine
from src.narration.script_parser import (
    NarrationScriptError,
    parse_narration_script,
    spoken_text,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_script(path: Path, beat_ids: list[str]) -> None:
    lines = [
        "# Test episode",
        "",
        "## Narração final",
        "",
        "### Editorial section",
        "",
        "- **Metadata:** not spoken",
        "",
    ]
    for index, beat_id in enumerate(beat_ids, start=1):
        lines.extend(
            [
                f"### {beat_id} — Beat {index}",
                "",
                f"> Exact spoken text {index}.",
                "",
            ]
        )
    lines.extend(["## Notes", "", "Editorial only."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class OfficialNarrationScriptParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="official-script-parser-"
        )
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_seven_beats_are_dynamic_and_valid(self) -> None:
        script = self.directory / "seven.md"
        _write_script(script, [f"B{index:03d}" for index in range(1, 8)])

        beats = parse_narration_script(script)

        self.assertEqual(len(beats), 7)
        self.assertEqual(beats[-1]["beat_id"], "B007")
        self.assertEqual(beats[0]["word_count"], 4)

    def test_sixty_one_beats_are_dynamic_and_valid(self) -> None:
        script = self.directory / "sixty-one.md"
        _write_script(script, [f"B{index:03d}" for index in range(1, 62)])

        beats = parse_narration_script(script)

        self.assertEqual(len(beats), 61)
        self.assertEqual(beats[-1]["beat_id"], "B061")

    def test_duplicate_id_fails(self) -> None:
        script = self.directory / "duplicate.md"
        _write_script(script, ["B001", "B001"])
        with self.assertRaisesRegex(NarrationScriptError, "duplicate beat IDs"):
            parse_narration_script(script)

    def test_sequence_gap_fails(self) -> None:
        script = self.directory / "gap.md"
        _write_script(script, [
            "B001", "B002", "B003", "B004", "B005", "B006", "B007", "B009"
        ])
        with self.assertRaisesRegex(NarrationScriptError, "expected B008, got B009"):
            parse_narration_script(script)

    def test_unmarked_spoken_text_fails_closed(self) -> None:
        script = self.directory / "unmarked.md"
        _write_script(script, ["B001"])
        value = script.read_text(encoding="utf-8")
        script.write_text(
            value.replace("## Notes", "This could be spoken by mistake.\n\n## Notes"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(NarrationScriptError, "UNMARKED_NARRATION_TEXT"):
            parse_narration_script(script)

    def test_compiled_beat_fields_and_hash_depend_on_exact_speech(self) -> None:
        script = self.directory / "hash.md"
        _write_script(script, ["B001"])

        beat = parse_narration_script(script)[0]

        self.assertEqual(
            set(beat),
            {"beat_id", "title", "raw_text", "source_file", "source_hash", "word_count"},
        )
        self.assertEqual(
            beat["source_hash"],
            hashlib.sha256(beat["raw_text"].encode("utf-8")).hexdigest(),
        )

    def test_co001_spoken_text_is_byte_identical_after_migration(self) -> None:
        original_spoken_hash = (
            ROOT / "tests" / "fixtures" / "co001_spoken_before_migration.sha256"
        ).read_text(encoding="utf-8").strip()
        migrated_spoken_text = spoken_text(
            parse_narration_script(config.DEFAULT_INPUT)
        )

        self.assertEqual(
            hashlib.sha256(migrated_spoken_text.encode("utf-8")).hexdigest(),
            original_spoken_hash,
        )

    def test_official_dry_run_uses_script_not_pilot_and_never_calls_azure(self) -> None:
        plan_path = self.directory / "narration_plan.json"
        forbidden_wav = self.directory / "must-not-exist.wav"
        with (
            mock.patch.object(engine, "BEATS_DATA", []),
            mock.patch.object(engine.azure_sdk, "synthesize") as azure,
        ):
            exit_code = project_main.main(
                [
                    "narracao",
                    "--official",
                    "--dry-run",
                    "--narration-plan",
                    str(plan_path),
                    "--saida",
                    str(forbidden_wav),
                ]
            )

        self.assertEqual(exit_code, 0)
        azure.assert_not_called()
        self.assertFalse(forbidden_wav.exists())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["episode_id"], config.ACTIVE_EPISODE.episode_id)
        self.assertEqual(plan["scope"], "official_narration")
        self.assertIs(plan["official_narration"], True)
        self.assertEqual(plan["beat_count"], 38)
        self.assertEqual(plan["word_count"], 1009)
        self.assertEqual(plan["source_file"], config.relative_path(config.DEFAULT_INPUT))
        self.assertEqual(plan["voice"], "pt-BR-HumbertoNeural")
        self.assertEqual(plan["sample_rate"], 48000)
        self.assertEqual(plan["output_format"], "riff-48khz-16bit-mono-pcm")
        self.assertEqual(plan["rate"], "-7%")
        self.assertEqual(plan["pitch"], "0%")
        self.assertEqual(len(plan["source_sha256"]), 64)
        self.assertEqual(len(plan["beats"]), plan["beat_count"])

    def test_official_rejects_partial_beat_selection(self) -> None:
        plan_path = self.directory / "must-not-exist.json"
        with mock.patch.object(engine.azure_sdk, "synthesize") as azure:
            exit_code = project_main.main(
                [
                    "narracao",
                    "--official",
                    "--dry-run",
                    "--beats",
                    "B001",
                    "--narration-plan",
                    str(plan_path),
                ]
            )
        self.assertEqual(exit_code, 2)
        azure.assert_not_called()
        self.assertFalse(plan_path.exists())


if __name__ == "__main__":
    unittest.main()

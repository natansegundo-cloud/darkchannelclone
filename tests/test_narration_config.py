from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
import unittest
from unittest import mock

from src.episodes import resolve_active_episode
from src.narration import config, engine
from src.narration.providers.pacing import BEATS_DATA, BEATS_METADATA
import src.narration.providers.pacing as pacing_module
import src.narration.validation as narration_validation
from src.narration.validation import (
    CANONICAL_PITCH,
    CANONICAL_RATE,
    CANONICAL_VOICE,
    PILOT_BEATS_SCOPE,
    apply_operational_delivery,
    validate_delivery_contract,
    validate_pilot_narration,
)


ROOT = Path(__file__).resolve().parents[1]


class NarrationConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.narrators = config.load_narrators()
        self.motion = config.load_motion_contract()

    def test_canonical_delivery_is_owned_by_motion_contract(self) -> None:
        pacing = self.motion["voice_pacing"]
        narrator = config.default_narrator()

        self.assertEqual(pacing["voice"], CANONICAL_VOICE)
        self.assertEqual(pacing["rate"], CANONICAL_RATE)
        self.assertEqual(pacing["pitch"], CANONICAL_PITCH)
        self.assertEqual(narrator["voice"], pacing["voice"])
        self.assertEqual(narrator["delivery"]["rate"], pacing["rate"])
        self.assertEqual(narrator["delivery"]["pitch"], pacing["pitch"])
        self.assertEqual(validate_delivery_contract(self.narrators, self.motion), [])

        divergent_runtime_copy = deepcopy(narrator)
        divergent_runtime_copy["voice"] = "temporary-wrong-voice"
        divergent_runtime_copy["delivery"]["rate"] = "+20%"
        resolved = apply_operational_delivery(divergent_runtime_copy, self.motion)
        self.assertEqual(resolved["voice"], CANONICAL_VOICE)
        self.assertEqual(resolved["delivery"]["rate"], CANONICAL_RATE)
        self.assertEqual(resolved["delivery"]["pitch"], CANONICAL_PITCH)

    def test_delivery_divergence_fails_validation(self) -> None:
        mutations = (
            ("voice", "pt-BR-WrongVoice"),
            ("rate", "-4%"),
            ("pitch", "+2%"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                narrators = deepcopy(self.narrators)
                narrator = narrators["narrators"][0]
                if field == "voice":
                    narrator[field] = value
                else:
                    narrator["delivery"][field] = value
                self.assertTrue(validate_delivery_contract(narrators, self.motion))

    def test_pilot_beats_are_never_labeled_full_script(self) -> None:
        self.assertEqual(BEATS_METADATA["scope"], PILOT_BEATS_SCOPE)
        self.assertEqual(BEATS_METADATA["coverage"], "partial")
        self.assertIs(BEATS_METADATA["official_narration"], False)
        self.assertEqual(
            validate_pilot_narration(
                BEATS_DATA,
                BEATS_METADATA,
                self.motion,
                production_stage="visual_qualification",
            ),
            [],
        )
        self.assertNotIn("full_script", inspect.getsource(pacing_module))
        self.assertNotIn('"scope": "full_script"', inspect.getsource(engine))

    def test_pilot_validation_has_no_universal_beat_total(self) -> None:
        for count in (1, 21):
            beats = [
                {"beat_id": f"P{index + 1:03d}", "pause_after_ms": 400}
                for index in range(count)
            ]
            with self.subTest(count=count):
                self.assertEqual(
                    validate_pilot_narration(
                        beats,
                        BEATS_METADATA,
                        self.motion,
                        production_stage="visual_qualification",
                    ),
                    [],
                )

    def test_active_episode_defaults_and_cli_overrides_are_dynamic(self) -> None:
        episode = resolve_active_episode(ROOT)
        self.assertEqual(config.DEFAULT_INPUT, episode.file("roteiro_narracao.md"))
        self.assertEqual(
            config.DEFAULT_OUTPUT,
            ROOT / "output" / "audio" / episode.episode_id,
        )
        self.assertNotIn("CO-001", inspect.getsource(config))

        explicit_input = ROOT / "custom-script.md"
        explicit_output = ROOT / "custom-output.wav"
        args = engine.build_parser().parse_args(
            ["--entrada", str(explicit_input), "--saida", str(explicit_output)]
        )
        self.assertEqual(args.entrada, explicit_input)
        self.assertEqual(args.saida, explicit_output)

    def test_configuration_validation_never_calls_azure(self) -> None:
        with mock.patch.object(engine.azure_sdk, "synthesize") as azure:
            self.assertEqual(validate_delivery_contract(self.narrators, self.motion), [])
            self.assertEqual(
                validate_pilot_narration(
                    BEATS_DATA,
                    BEATS_METADATA,
                    self.motion,
                    production_stage="visual_qualification",
                ),
                [],
            )
        azure.assert_not_called()

    def test_visual_qualification_does_not_require_complete_narration(self) -> None:
        one_pilot_beat = [{"beat_id": "P001", "pause_after_ms": 400}]
        self.assertEqual(
            validate_pilot_narration(
                one_pilot_beat,
                BEATS_METADATA,
                self.motion,
                production_stage="visual_qualification",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

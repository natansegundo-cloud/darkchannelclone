from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import src.episodes as episodes
import src.visuals.engine as visual_engine
from src.episodes import (
    EpisodeConfigError,
    load_json,
    load_visual_script,
    resolve_active_episode,
    validate_scene_map,
    validate_visual_script,
)
from src.visuals.engine import load_visual_scenes, validate_visual_scenes


def planned_rows(count: int) -> list[dict[str, str]]:
    return [
        {
            "scene_id": f"S{index + 1:03d}",
            "start": str(index * 5),
            "end": str((index + 1) * 5),
            "scene_type": "CHARACTER_SCENE",
            "format": "image",
        }
        for index in range(count)
    ]


def planned_ids(rows: list[dict[str, str]]) -> list[str]:
    return [row["scene_id"] for row in rows]


def planned_types(rows: list[dict[str, str]]) -> dict[str, str]:
    return {row["scene_id"]: row["scene_type"] for row in rows}


class DynamicEpisodeValidationTest(unittest.TestCase):
    def test_active_episode_is_resolved_from_project_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dynamic-episode-") as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            episode_dir = root / "episodios" / "EP-DYNAMIC"
            episode_dir.mkdir(parents=True)
            (root / "config" / "project.json").write_text(
                json.dumps({"active_episode": "EP-DYNAMIC"}),
                encoding="utf-8",
            )
            (episode_dir / "episodio.json").write_text(
                json.dumps(
                    {
                        "episodio_id": "EP-DYNAMIC",
                        "production_stage": "visual_qualification",
                    }
                ),
                encoding="utf-8",
            )

            context = resolve_active_episode(root)

            self.assertEqual(context.episode_id, "EP-DYNAMIC")
            self.assertEqual(context.episode_dir, episode_dir.resolve())

    def test_active_episode_metadata_mismatch_fails_with_clear_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="episode-mismatch-") as temporary:
            root = Path(temporary)
            episode_dir = root / "episodios" / "EP-ACTIVE"
            (root / "config").mkdir(parents=True)
            episode_dir.mkdir(parents=True)
            (root / "config" / "project.json").write_text(
                json.dumps({"active_episode": "EP-ACTIVE"}), encoding="utf-8"
            )
            (episode_dir / "episodio.json").write_text(
                json.dumps(
                    {
                        "episodio_id": "EP-OTHER",
                        "production_stage": "visual_qualification",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EpisodeConfigError, "EPISODE_ID_MISMATCH"):
                resolve_active_episode(root)

    def test_seven_scene_episode_is_valid(self) -> None:
        self.assertEqual(validate_visual_script(planned_rows(7)), [])

    def test_sixty_three_scene_episode_is_valid(self) -> None:
        self.assertEqual(validate_visual_script(planned_rows(63)), [])

    def test_visual_qualification_allows_partial_visual_scenes_and_scene_map(self) -> None:
        canonical = load_visual_scenes()
        rows = planned_rows(7)
        partial_visuals = deepcopy(canonical)
        partial_visuals["episode_id"] = "TEST-EPISODE"
        partial_visuals["scenes"] = partial_visuals["scenes"][:3]
        self.assertEqual(
            validate_visual_scenes(
                partial_visuals,
                expected_episode_id="TEST-EPISODE",
                planned_scene_ids=planned_ids(rows),
                planned_scene_types=planned_types(rows),
                production_stage="visual_qualification",
            ),
            [],
        )

        partial_map = {
            "episode_id": "TEST-EPISODE",
            "timing_quality": "WORD_BOUNDARY_REAL",
            "visual_direction": "FIN_AUDIENCE_PROXY_SITUATIONAL",
            "scenes": [
                {
                    "scene_id": row["scene_id"],
                    "scene_type": row["scene_type"],
                    "start": float(row["start"]),
                    "end": float(row["end"]),
                    "visual_intent": "Declared visual intent",
                    "setting": "Declared setting",
                }
                for row in rows[:2]
            ],
        }
        self.assertEqual(
            validate_scene_map(
                partial_map,
                expected_episode_id="TEST-EPISODE",
                planned_scene_ids=planned_ids(rows),
                planned_scene_types=planned_types(rows),
                production_stage="visual_qualification",
                expected_timing_quality="WORD_BOUNDARY_REAL",
            ),
            [],
        )
        self.assertTrue(
            validate_visual_scenes(
                partial_visuals,
                expected_episode_id="TEST-EPISODE",
                planned_scene_ids=planned_ids(rows),
                planned_scene_types=planned_types(rows),
                production_stage="production",
            )
        )

    def test_duplicate_scene_ids_fail(self) -> None:
        rows = planned_rows(7)
        rows[1]["scene_id"] = rows[0]["scene_id"]
        errors = validate_visual_script(rows)
        self.assertTrue(any("duplicate scene_ids" in error for error in errors))

    def test_declared_complete_scene_map_cannot_have_partial_coverage(self) -> None:
        rows = planned_rows(7)
        payload = {
            "episode_id": "TEST-EPISODE",
            "timing_quality": "WORD_BOUNDARY_REAL",
            "visual_direction": "FIN_AUDIENCE_PROXY_SITUATIONAL",
            "status": "COMPLETE",
            "scenes": [
                {
                    "scene_id": row["scene_id"],
                    "scene_type": row["scene_type"],
                    "start": float(row["start"]),
                    "end": float(row["end"]),
                    "visual_intent": "Declared visual intent",
                    "setting": "Declared setting",
                }
                for row in rows[:3]
            ],
        }
        errors = validate_scene_map(
            payload,
            expected_episode_id="TEST-EPISODE",
            planned_scene_ids=planned_ids(rows),
            planned_scene_types=planned_types(rows),
            production_stage="visual_qualification",
        )
        self.assertTrue(any("declared COMPLETE coverage" in error for error in errors))

    def test_visual_scene_absent_from_script_fails(self) -> None:
        canonical = load_visual_scenes()
        rows = planned_rows(7)
        payload = deepcopy(canonical)
        payload["episode_id"] = "TEST-EPISODE"
        payload["scenes"] = [deepcopy(payload["scenes"][0])]
        payload["scenes"][0]["scene_id"] = "S999"
        errors = validate_visual_scenes(
            payload,
            expected_episode_id="TEST-EPISODE",
            planned_scene_ids=planned_ids(rows),
            planned_scene_types=planned_types(rows),
            production_stage="visual_qualification",
        )
        self.assertTrue(any("absent from roteiro_visual" in error for error in errors))

    def test_visual_scene_relative_order_must_match_script(self) -> None:
        canonical = load_visual_scenes()
        rows = planned_rows(7)
        payload = deepcopy(canonical)
        payload["episode_id"] = "TEST-EPISODE"
        payload["scenes"] = [
            deepcopy(payload["scenes"][1]),
            deepcopy(payload["scenes"][0]),
        ]
        errors = validate_visual_scenes(
            payload,
            expected_episode_id="TEST-EPISODE",
            planned_scene_ids=planned_ids(rows),
            planned_scene_types=planned_types(rows),
            production_stage="visual_qualification",
        )
        self.assertTrue(any("relative order" in error for error in errors))

    def test_validation_code_has_no_fixed_scene_count_or_duration(self) -> None:
        sources = "\n".join(
            [
                inspect.getsource(episodes),
                inspect.getsource(visual_engine),
                (episodes.ROOT / "scripts" / "validar_projeto.py").read_text(
                    encoding="utf-8-sig"
                ),
            ]
        )
        for forbidden in (
            "range(1, 19)",
            "range(1, 49)",
            "S001-S018",
            "S001-S048",
            "!= 18",
            "!= 48",
            "== 18",
            "== 48",
            "181.992773",
        ):
            self.assertNotIn(forbidden, sources)

    def test_current_active_episode_remains_valid(self) -> None:
        context = resolve_active_episode(episodes.ROOT)
        rows = load_visual_script(context.file("roteiro_visual.csv"))
        ids = planned_ids(rows)
        types = planned_types(rows)
        self.assertEqual(context.production_stage, "visual_qualification")
        self.assertEqual(context.metadata["visual_qualification"]["status"], "PASSED")
        self.assertEqual(validate_visual_script(rows), [])
        self.assertEqual(
            validate_scene_map(
                load_json(context.file("scene_map.json")),
                expected_episode_id=context.episode_id,
                planned_scene_ids=ids,
                planned_scene_types=types,
                production_stage=context.production_stage,
                expected_timing_quality=str(context.project["timing_quality"]),
            ),
            [],
        )
        self.assertEqual(
            validate_visual_scenes(
                load_json(context.file("visual_scenes.json")),
                expected_episode_id=context.episode_id,
                planned_scene_ids=ids,
                planned_scene_types=types,
                production_stage=context.production_stage,
            ),
            [],
        )

    def test_validation_never_opens_a_network_connection(self) -> None:
        rows = planned_rows(7)
        original_connection = socket.create_connection
        with mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("validation attempted network access"),
        ):
            self.assertEqual(validate_visual_script(rows), [])
        self.assertIs(socket.create_connection, original_connection)


if __name__ == "__main__":
    unittest.main()

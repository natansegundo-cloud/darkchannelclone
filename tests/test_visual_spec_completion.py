from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from src.episodes import load_json, load_visual_script, resolve_active_episode
from src.visuals.data_visual import validate_data_visual
from src.visuals.engine import build_generation_jobs, load_reference_profile
from src.visuals.prompt_builder import build_prompt
from src.visuals.references import resolve_scene_references
from src.visuals.spec_preview import preview_visual_specs


ROOT = Path(__file__).resolve().parents[1]
APPROVED_FIRST_18_SHA256 = "e2cecabf20d917ed240934297f13528f19a5df9d8eae610f33fe81565dbc85b7"


class VisualSpecCompletionTest(unittest.TestCase):
    def setUp(self):
        self.episode = resolve_active_episode(ROOT)
        self.visual_path = self.episode.file("visual_scenes.json")
        self.script_path = self.episode.file("roteiro_visual.csv")
        self.map_path = self.episode.file("scene_map.json")
        self.scenes = build_generation_jobs(visual_scenes_path=self.visual_path)

    def test_approved_first_eighteen_are_preserved(self):
        encoded = json.dumps(
            self.scenes[:18], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), APPROVED_FIRST_18_SHA256)

    def test_dynamic_coverage_matches_editorial_and_scene_map(self):
        editorial_ids = [row["scene_id"] for row in load_visual_script(self.script_path)]
        map_ids = [scene["scene_id"] for scene in load_json(self.map_path)["scenes"]]
        visual_ids = [scene["scene_id"] for scene in self.scenes]
        self.assertEqual(visual_ids, editorial_ids)
        self.assertEqual(visual_ids, map_ids)
        self.assertEqual(len(visual_ids), len(set(visual_ids)))
        self.assertEqual(len(visual_ids), 48)  # Current CO-001 result, not an engine constant.

    def test_static_quality_prompts_policies_data_and_conditional_references(self):
        profile = load_reference_profile()
        fin_refs = set(profile["required_references"])
        for scene in self.scenes:
            with self.subTest(scene=scene["scene_id"]):
                self.assertTrue(scene["dominant_idea"].strip())
                self.assertTrue(scene["situation"].strip())
                self.assertTrue(scene["action"].strip())
                self.assertIn(scene["text_policy"]["mode"], {"NONE", "OVERLAY"})
                if scene["scene_type"] == "CHARACTER_SCENE":
                    self.assertEqual(scene["character_presence"], "FIN")
                if scene["scene_type"] == "SIMPLE_DATA_SCENE":
                    self.assertEqual(validate_data_visual(scene["data_visual"]), [])
                refs = set(resolve_scene_references(scene, profile))
                if scene["character_presence"] == "FIN":
                    self.assertTrue(fin_refs.issubset(refs))
                else:
                    self.assertTrue(fin_refs.isdisjoint(refs))
                prompt = build_prompt(scene)
                self.assertTrue(prompt.strip())
                self.assertNotIn("\n", prompt)

    def test_offline_routing_and_cost_preview_never_calls_provider(self):
        baseline_missing = load_json(self.map_path)["missing_visual_scene_ids"]
        with mock.patch("src.visuals.providers.OpenRouterImageProvider.generate") as generate:
            preview = preview_visual_specs(
                visual_scenes_path=self.visual_path,
                visual_script_path=self.script_path,
                scene_map_path=self.map_path,
                output_dir=ROOT / "output" / "generated_images",
                cost_scene_ids=baseline_missing,
            )
        generate.assert_not_called()
        self.assertEqual(preview["compiled_prompts"], len(self.scenes))
        self.assertEqual(preview["routing_preview"], {
            "FLUX": 41, "GPT_IMAGE": 6, "LOCAL_DETERMINISTIC": 1,
        })
        self.assertEqual(preview["estimated_generation_cost_usd"], 0.84)
        self.assertEqual(preview["external_calls"], 0)

    def test_current_classification_counts(self):
        presence = Counter(scene["character_presence"] for scene in self.scenes)
        scene_types = Counter(scene["scene_type"] for scene in self.scenes)
        policies = Counter(scene["text_policy"]["mode"] for scene in self.scenes)
        self.assertEqual(presence, {"FIN": 41, "NONE": 7})
        self.assertEqual(scene_types["SIMPLE_DATA_SCENE"], 1)
        self.assertEqual(policies, {"NONE": 47, "OVERLAY": 1})


if __name__ == "__main__":
    unittest.main()

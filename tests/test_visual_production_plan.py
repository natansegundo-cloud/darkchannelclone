from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from src.episodes import load_visual_script, resolve_active_episode
from src.visuals.engine import load_generation_config
from src.visuals.production_plan import validate_visual_production_plan


ROOT = Path(__file__).resolve().parents[1]
class VisualProductionPlanTest(unittest.TestCase):
    def setUp(self):
        self.episode = resolve_active_episode(ROOT)
        self.plan = json.loads(self.episode.file("visual_production_plan.json").read_text(encoding="utf-8"))
        self.scene_ids = [row["scene_id"] for row in load_visual_script(self.episode.file("roteiro_visual.csv"))]

    def errors(self, payload):
        return validate_visual_production_plan(
            payload, episode_id=self.episode.episode_id,
            planned_scene_ids=self.scene_ids,
            generation_config=load_generation_config(), project_root=ROOT,
        )

    def test_plan_is_complete_dynamic_and_valid(self):
        self.assertEqual(self.errors(self.plan), [])
        assigned = [row["scene_id"] for sequence in self.plan["sequences"] for row in sequence["derivations"]]
        self.assertEqual(assigned, self.scene_ids)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(self.plan["summary"]["unassigned_scenes"], 0)

    def test_optimized_cost_uses_local_prices_and_meets_main_target(self):
        cost = self.plan["cost_model"]
        self.assertEqual(cost["current_plan"]["paid_generations"], 30)
        self.assertEqual(cost["current_plan"]["estimated_cost"], 0.84)
        self.assertEqual(cost["optimized_plan"]["paid_generations"], 8)
        self.assertEqual(cost["optimized_plan"]["estimated_cost"], 0.16)
        self.assertEqual(cost["optimized_plan"]["target_usd_0_20"], "PASS")
        self.assertEqual(cost["optimized_plan"]["stretch_target_usd_0_15"], "FAIL")

    def test_only_approved_existing_assets_are_used(self):
        approved = {asset["scene_id"] for asset in self.plan["approved_assets"]}
        self.assertNotIn("S016", approved)
        for sequence in self.plan["sequences"]:
            if sequence["base_source"] == "EXISTING":
                self.assertIn(sequence["base_scene"], approved)

    def test_invalid_coverage_and_cost_fail_closed(self):
        missing = deepcopy(self.plan)
        missing["sequences"][-1]["derivations"] = []
        self.assertTrue(self.errors(missing))
        wrong_cost = deepcopy(self.plan)
        wrong_cost["cost_model"]["optimized_plan"]["estimated_cost"] = 0.01
        self.assertTrue(self.errors(wrong_cost))

if __name__ == "__main__":
    unittest.main()

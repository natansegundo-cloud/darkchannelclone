from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.episodes import load_visual_script, resolve_active_episode
from src.visuals.scene_map import SceneMapError, build_scene_map


ROOT = Path(__file__).resolve().parents[1]


class SceneMapTest(unittest.TestCase):
    def test_official_map_has_dynamic_complete_word_boundary_coverage(self):
        episode = resolve_active_episode(ROOT)
        rows = load_visual_script(episode.file("roteiro_visual.csv"))
        timing_path = ROOT / "output" / "audio" / episode.episode_id / "timing.json"
        payload = build_scene_map(
            visual_script_path=episode.file("roteiro_visual.csv"),
            visual_scenes_path=episode.file("visual_scenes.json"),
            timing_path=timing_path,
            narration_path=episode.file("roteiro_narracao.md"),
            existing_scene_map_path=episode.file("scene_map.json"),
        )
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["scene_count"], len(rows))
        self.assertEqual([scene["scene_id"] for scene in payload["scenes"]], [row["scene_id"] for row in rows])
        self.assertEqual(payload["timing_quality"], "WORD_BOUNDARY_REAL")
        self.assertEqual(payload["scenes"][0]["start"], 0.0)
        self.assertEqual(payload["scenes"][-1]["end"], timing["duration_seconds"])
        self.assertTrue(all(scene["duration"] > 0 for scene in payload["scenes"]))
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(payload["scenes"], payload["scenes"][1:])))
        self.assertTrue(all(scene["beat_ids"] for scene in payload["scenes"]))
        semantic = [scene for scene in payload["scenes"] if scene["anchor_resolution"] == "SEMANTIC_UNIQUE_SUBPHRASE"]
        self.assertEqual([(scene["scene_id"], scene["resolved_anchor"]) for scene in semantic], [("S019", "numero magico")])

    def test_scene_count_and_timing_rebuild_are_not_fixed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            csv_path = root / "roteiro_visual.csv"
            fields = ["scene_id", "narration_anchor", "scene_type", "visual", "motion"]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for scene_id, anchor in (("S001", "Alpha"), ("S002", "Gamma"), ("S003", "Epsilon")):
                    writer.writerow({"scene_id": scene_id, "narration_anchor": anchor, "scene_type": "OBJECT_SCENE", "visual": "object", "motion": "HOLD"})
            visual_path = root / "visual_scenes.json"
            visual_path.write_text(json.dumps({"episode_id": "TEST", "scenes": []}), encoding="utf-8")
            narration_path = root / "roteiro_narracao.md"
            narration_path.write_text("Alpha Beta Gamma Delta Epsilon", encoding="utf-8")
            timing_path = root / "timing.json"

            def timing(duration, starts):
                words = [
                    {"text": text, "normalized": text.lower(), "start": start, "end": start + .5}
                    for text, start in zip(("Alpha", "Beta", "Gamma", "Delta", "Epsilon"), starts)
                ]
                timing_path.write_text(json.dumps({
                    "episode_id": "TEST", "timing_quality": "WORD_BOUNDARY_REAL",
                    "same_synthesis_audio_timing": True, "duration_seconds": duration,
                    "beats": [{"beat_id": "B001", "words": words}],
                }), encoding="utf-8")

            timing(10.0, [1, 2, 4, 5, 7])
            first = build_scene_map(visual_script_path=csv_path, visual_scenes_path=visual_path, timing_path=timing_path, narration_path=narration_path)
            self.assertEqual(first["scene_count"], 3)
            self.assertEqual([(s["start"], s["end"]) for s in first["scenes"]], [(0.0, 4.0), (4.0, 7.0), (7.0, 10.0)])
            timing(12.0, [1, 3, 5, 7, 9])
            rebuilt = build_scene_map(visual_script_path=csv_path, visual_scenes_path=visual_path, timing_path=timing_path, narration_path=narration_path)
            self.assertEqual([(s["start"], s["end"]) for s in rebuilt["scenes"]], [(0.0, 5.0), (5.0, 9.0), (9.0, 12.0)])

    def test_missing_anchor_fails_closed(self):
        episode = resolve_active_episode(ROOT)
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            path = Path(name) / "roteiro_visual.csv"
            path.write_text("scene_id,narration_anchor,scene_type,visual,motion\nS001,frase inexistente xyz,OBJECT_SCENE,objeto,HOLD\n", encoding="utf-8")
            with self.assertRaises(SceneMapError):
                build_scene_map(
                    visual_script_path=path,
                    visual_scenes_path=episode.file("visual_scenes.json"),
                    timing_path=ROOT / "output" / "audio" / episode.episode_id / "timing.json",
                    narration_path=episode.file("roteiro_narracao.md"),
                )


if __name__ == "__main__":
    unittest.main()

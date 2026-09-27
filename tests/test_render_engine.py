from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import main as project_main

from src.render.engine import (
    FINAL_RENDER_BLOCKED,
    FinalRenderBlocked,
    RenderError,
    build_ffmpeg_command,
    build_render_plan,
    execute_final_render,
)
from src.render.preflight import run_preflight


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_wav(path: Path, duration: float, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * round(duration * sample_rate))


def _project(
    tmp_path: Path,
    *,
    stage: str = "production",
    scene_count: int = 2,
    overlay: bool = False,
) -> dict[str, Path]:
    root = tmp_path / "project"
    episode_id = "EP-DYNAMIC"
    episode_dir = root / "episodios" / episode_id
    audio = root / "output" / "audio" / episode_id / "narration.wav"
    timing = audio.parent / "timing.json"
    manifest = root / "output" / "generated_images" / "manifest.json"
    contract = root / "config" / "motion_contract.json"
    duration = scene_count * 0.1

    _write_json(root / "config" / "project.json", {"active_episode": episode_id})
    _write_json(
        episode_dir / "episodio.json",
        {"episodio_id": episode_id, "production_stage": stage},
    )
    _write_json(contract, {"schema_version": "1.0", "contract_id": "TEST_MOTION"})
    (episode_dir / "roteiro_visual.csv").write_text(
        "scene_id\n" + "".join(f"SHOT-{index + 1}\n" for index in range(scene_count)),
        encoding="utf-8",
    )
    _write_wav(audio, duration)
    _write_json(
        timing,
        {
            "episode_id": episode_id,
            "scope": "official_narration",
            "official_narration": True,
            "timing_quality": "WORD_BOUNDARY_REAL",
            "same_synthesis_audio_timing": True,
            "duration_seconds": duration,
        },
    )

    motions = ["HOLD", "SLOW_ZOOM_IN", "PAN_LEFT", "LIGHT_PARALLAX"]
    mapped_scenes = []
    visual_scenes = []
    manifest_scenes = []
    for index in range(scene_count):
        scene_id = f"SHOT-{index + 1}"
        start = round(index * 0.1, 3)
        end = round((index + 1) * 0.1, 3)
        image = root / "output" / "generated_images" / f"{scene_id}.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"tiny-local-image-fixture")
        mapped_scenes.append(
            {
                "scene_id": scene_id,
                "start": start,
                "end": end,
                "motion": motions[index % len(motions)],
            }
        )
        visual_scenes.append(
            {
                "scene_id": scene_id,
                "text_policy": {
                    "mode": "OVERLAY" if overlay and index == 0 else "NONE"
                },
            }
        )
        manifest_scenes.append(
            {
                "scene_id": scene_id,
                "status": "generated",
                "review_status": "APPROVED",
                "final_path": image.relative_to(root).as_posix(),
            }
        )

    _write_json(
        episode_dir / "scene_map.json",
        {
            "episode_id": episode_id,
            "timing_quality": "WORD_BOUNDARY_REAL",
            "scenes": mapped_scenes,
        },
    )
    _write_json(
        episode_dir / "visual_scenes.json",
        {"episode_id": episode_id, "scenes": visual_scenes},
    )
    _write_json(manifest, {"episode_id": episode_id, "scenes": manifest_scenes})
    return {
        "root": root,
        "episode_dir": episode_dir,
        "audio": audio,
        "timing": timing,
        "manifest": manifest,
        "contract": contract,
    }


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ffmpeg_fixture(path: Path) -> Path:
    path.write_text("local test executable placeholder", encoding="utf-8")
    return path


def _version_runner(calls: list[list[str]]):
    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "ffmpeg version fixture", "")

    return run


class RenderEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_final_is_blocked_during_visual_qualification(self) -> None:
        paths = _project(self.tmp_path, stage="visual_qualification")
        with self.assertRaisesRegex(FinalRenderBlocked, FINAL_RENDER_BLOCKED):
            execute_final_render(root=paths["root"], dry_run=True)
        self.assertFalse((paths["root"] / "output" / "render").exists())

    def test_validate_runs_preflight_without_producing_video(self) -> None:
        paths = _project(self.tmp_path)
        result = run_preflight(root=paths["root"])
        self.assertTrue(result["passed"])
        self.assertFalse(list(paths["root"].rglob("*.mp4")))

    def test_wrong_timing_episode_fails_closed(self) -> None:
        paths = _project(self.tmp_path)
        timing = _load(paths["timing"])
        timing["episode_id"] = "ANOTHER-EPISODE"
        _write_json(paths["timing"], timing)
        result = run_preflight(root=paths["root"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("EPISODE_ID_MISMATCH" in error for error in result["errors"]))

    def test_wrong_scene_map_episode_fails_closed(self) -> None:
        paths = _project(self.tmp_path)
        scene_map_path = paths["episode_dir"] / "scene_map.json"
        scene_map = _load(scene_map_path)
        scene_map["episode_id"] = "ANOTHER-EPISODE"
        _write_json(scene_map_path, scene_map)
        result = run_preflight(root=paths["root"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("EPISODE_ID_MISMATCH" in error for error in result["errors"]))

    def test_wrong_visual_manifest_episode_fails_closed(self) -> None:
        paths = _project(self.tmp_path)
        manifest = _load(paths["manifest"])
        manifest["episode_id"] = "ANOTHER-EPISODE"
        _write_json(paths["manifest"], manifest)
        result = run_preflight(root=paths["root"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("EPISODE_ID_MISMATCH" in error for error in result["errors"]))

    def test_optional_artifact_episode_ids_are_cross_validated(self) -> None:
        paths = _project(self.tmp_path)
        narration_metadata = paths["audio"].parent / "narration.json"
        render_plan = paths["root"] / "output" / "render" / "EP-DYNAMIC" / "render_plan.json"
        _write_json(narration_metadata, {"episode_id": "ANOTHER-EPISODE"})
        _write_json(render_plan, {"episode_id": "ANOTHER-EPISODE"})
        visual_scenes_path = paths["episode_dir"] / "visual_scenes.json"
        visual_scenes = _load(visual_scenes_path)
        visual_scenes["episode_id"] = "ANOTHER-EPISODE"
        _write_json(visual_scenes_path, visual_scenes)

        result = run_preflight(root=paths["root"])

        mismatches = [error for error in result["errors"] if "EPISODE_ID_MISMATCH" in error]
        self.assertEqual(len(mismatches), 3)

    def test_preflight_identity_validation_never_uses_network(self) -> None:
        paths = _project(self.tmp_path)
        original_connection = socket.create_connection
        with mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("preflight attempted network access"),
        ):
            result = run_preflight(root=paths["root"])
        self.assertTrue(result["passed"])
        self.assertIs(socket.create_connection, original_connection)

    def test_missing_official_audio_fails(self) -> None:
        paths = _project(self.tmp_path)
        paths["audio"].unlink()
        result = run_preflight(root=paths["root"])
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("official narration audio not found" in error for error in result["errors"])
        )

    def test_missing_real_timing_fails(self) -> None:
        paths = _project(self.tmp_path)
        timing = _load(paths["timing"])
        timing["timing_quality"] = "HEURISTIC"
        _write_json(paths["timing"], timing)
        result = run_preflight(root=paths["root"])
        self.assertTrue(any("timing_quality" in error for error in result["errors"]))

    def test_pending_visual_fails(self) -> None:
        paths = _project(self.tmp_path)
        manifest = _load(paths["manifest"])
        manifest["scenes"][0]["status"] = "pending"
        _write_json(paths["manifest"], manifest)
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("visual status is not renderable" in error for error in result["errors"])
        )

    def test_missing_approved_visual_asset_fails(self) -> None:
        paths = _project(self.tmp_path)
        manifest = _load(paths["manifest"])
        missing_asset = paths["root"] / manifest["scenes"][0]["final_path"]
        missing_asset.unlink()
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("approved final visual asset does not exist" in error for error in result["errors"])
        )

    def test_unsupported_motion_fails(self) -> None:
        paths = _project(self.tmp_path)
        scene_map_path = paths["episode_dir"] / "scene_map.json"
        scene_map = _load(scene_map_path)
        scene_map["scenes"][0]["motion"] = "SPIN_FOREVER"
        _write_json(scene_map_path, scene_map)
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("unsupported motion preset" in error for error in result["errors"])
        )

    def test_timeline_gap_fails(self) -> None:
        paths = _project(self.tmp_path)
        scene_map_path = paths["episode_dir"] / "scene_map.json"
        scene_map = _load(scene_map_path)
        scene_map["scenes"][1]["start"] = 0.18
        _write_json(scene_map_path, scene_map)
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("timeline gap/overlap" in error for error in result["errors"])
        )

    def test_audio_duration_mismatch_fails(self) -> None:
        paths = _project(self.tmp_path)
        _write_wav(paths["audio"], 0.5)
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("does not match narration audio" in error for error in result["errors"])
        )

    def test_dry_run_writes_deterministic_plan_without_video_or_render_call(self) -> None:
        paths = _project(self.tmp_path, scene_count=4)
        fake_ffmpeg = _ffmpeg_fixture(self.tmp_path / "ffmpeg-fixture.exe")
        calls: list[list[str]] = []
        output_dir = self.tmp_path / "render-output"

        first = execute_final_render(
            root=paths["root"],
            output_dir=output_dir,
            ffmpeg=fake_ffmpeg,
            dry_run=True,
            runner=_version_runner(calls),
        )
        first_hash = hashlib.sha256((output_dir / "render_plan.json").read_bytes()).hexdigest()
        second = execute_final_render(
            root=paths["root"],
            output_dir=output_dir,
            ffmpeg=fake_ffmpeg,
            dry_run=True,
            runner=_version_runner(calls),
        )
        second_hash = hashlib.sha256((output_dir / "render_plan.json").read_bytes()).hexdigest()

        self.assertEqual(first["plan"], second["plan"])
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(len(first["plan"]["scenes"]), 4)
        self.assertEqual(calls, [[str(fake_ffmpeg.resolve()), "-version"]] * 2)
        self.assertFalse(list(output_dir.glob("*.mp4")))
        self.assertEqual(first["manifest"]["status"], "DRY_RUN")
        self.assertEqual(Path(first["manifest"]["output_file"]).name, "final.mp4")
        self.assertEqual(first["manifest"]["motion_contract"]["id"], "TEST_MOTION")
        self.assertEqual(first["manifest"]["ffmpeg"]["version"], "ffmpeg version fixture")
        self.assertEqual(first["manifest"]["ffmpeg"]["command"], first["command"])
        self.assertIsNotNone(first["manifest"]["narration"]["sha256"])
        self.assertIsNotNone(first["manifest"]["timing"]["sha256"])
        self.assertIsNotNone(first["manifest"]["scene_map"]["sha256"])
        self.assertIsNotNone(first["manifest"]["visual_manifest"]["sha256"])

    def test_missing_ffmpeg_fails_in_a_controlled_way(self) -> None:
        paths = _project(self.tmp_path)
        with self.assertRaisesRegex(RenderError, "FFmpeg is unavailable"):
            execute_final_render(
                root=paths["root"],
                output_dir=self.tmp_path / "render-output",
                ffmpeg=self.tmp_path / "missing-ffmpeg.exe",
                dry_run=True,
            )

    def test_plan_and_command_use_delivery_conventions(self) -> None:
        paths = _project(self.tmp_path, scene_count=1)
        preflight = run_preflight(root=paths["root"], require_production=True)
        plan = build_render_plan(preflight)
        command = build_ffmpeg_command(
            preflight,
            ffmpeg_executable="ffmpeg-fixture",
            output_path=self.tmp_path / "final.mp4",
        )
        self.assertEqual(plan["resolution"], {"width": 1920, "height": 1080})
        self.assertEqual(plan["frame_rate"], 30)
        self.assertEqual(plan["video_codec"], "H.264")
        self.assertEqual(plan["audio_codec"], "AAC")
        self.assertEqual(plan["scenes"][0]["review_status"], "APPROVED")
        self.assertIn("image_path", plan["scenes"][0])
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertTrue(command[-1].endswith("final.mp4"))
        self.assertTrue(all(not isinstance(part, bytes) for part in command))

    def test_overlay_requirement_fails_closed(self) -> None:
        paths = _project(self.tmp_path, overlay=True)
        result = run_preflight(root=paths["root"])
        self.assertTrue(
            any("overlay is required but not implemented" in error for error in result["errors"])
        )

    def test_renderer_has_no_scene_id_or_total_duration_hardcoding(self) -> None:
        render_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(__file__).parents[1] / "src" / "render").glob("*.py")
        )
        self.assertNotIn("S018", render_source)
        self.assertNotIn("S048", render_source)
        self.assertNotIn("TOTAL_SCENES", render_source)
        self.assertNotIn("TOTAL_DURATION", render_source)

    def test_existing_main_subcommands_still_parse(self) -> None:
        parser = project_main.build_parser()
        self.assertEqual(parser.parse_args(["narracao"]).command, "narracao")
        self.assertEqual(parser.parse_args(["visuals", "validate"]).command, "visuals")
        self.assertEqual(parser.parse_args(["render", "validate"]).command, "render")


if __name__ == "__main__":
    unittest.main()

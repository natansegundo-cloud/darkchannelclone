from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
import inspect
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from src.episodes import (
    load_json as load_episode_json,
    load_visual_script,
    resolve_active_episode,
    validate_scene_map,
    validate_visual_script,
)
from src.visuals.engine import (
    ALLOWED_SCENE_FIELDS,
    REQUIRED_SCENE_FIELDS,
    build_generation_jobs,
    load_generation_config,
    load_reference_profile,
    load_visual_scenes,
    validate_visual_scenes,
)
from src.visuals.generation_runner import approve_scenes, run_generation, upgrade_scenes
from src.visuals.deterministic_renderer import (
    CANVAS_SIZE,
    official_background_rgb,
    render_deterministic_scene,
)
import src.visuals.deterministic_renderer as deterministic_renderer
import src.visuals.prompt_builder as prompt_builder
import src.visuals.routing as visual_routing
from src.visuals.prompt_builder import TEXT_POLICY_MODES, build_prompt, build_source_prompt
from src.visuals.providers import (
    GenerationRequest,
    GenerationResult,
    MockVisualProvider,
    OpenRouterImageProvider,
    ProviderRegistry,
    UNKNOWN_BILLED_TIMEOUT,
)
from src.visuals.routing import resolve_visual_route
from src.visuals.validators import (
    SCENE_MANIFEST_FIELDS,
    validate_compiled_prompt,
    validate_generation_config,
    validate_output,
    validate_reference_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
def _png_rgb_counts(path: Path) -> tuple[tuple[int, int], Counter[tuple[int, int, int]]]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("expected PNG signature")
    offset = 8
    compressed = bytearray()
    width = height = 0
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = width * 3
    colors: Counter[tuple[int, int, int]] = Counter()
    for row_index in range(height):
        row_start = row_index * (stride + 1)
        if raw[row_start] != 0:
            raise AssertionError("expected unfiltered PNG scanline")
        row = raw[row_start + 1 : row_start + 1 + stride]
        colors.update(zip(row[0::3], row[1::3], row[2::3]))
    return (width, height), colors


class CanonicalVisualScenesTest(unittest.TestCase):
    def test_visual_scenes_json_is_valid_and_unique(self) -> None:
        payload = load_visual_scenes()
        scenes = payload["scenes"]

        self.assertEqual(validate_visual_scenes(payload), [])
        episode = resolve_active_episode(ROOT)
        planned_rows = load_visual_script(episode.file("roteiro_visual.csv"))
        planned_ids = [row["scene_id"] for row in planned_rows]
        scene_ids = [scene["scene_id"] for scene in scenes]
        self.assertEqual(payload["episode_id"], episode.episode_id)
        self.assertEqual(payload["character_lock"], "FIN_V1")
        self.assertEqual(payload["visual_profile"], "ILLUSTRATED_V1")
        self.assertTrue(scene_ids)
        self.assertEqual(len(scene_ids), len(set(scene_ids)))
        self.assertTrue(set(scene_ids).issubset(planned_ids))
        self.assertEqual(
            [planned_ids.index(scene_id) for scene_id in scene_ids],
            sorted(planned_ids.index(scene_id) for scene_id in scene_ids),
        )

    def test_scenes_contain_only_variable_fields_and_valid_limits(self) -> None:
        for scene in load_visual_scenes()["scenes"]:
            expected_fields = set(REQUIRED_SCENE_FIELDS)
            if scene["scene_type"] == "SIMPLE_DATA_SCENE":
                expected_fields.add("data_visual")
            self.assertEqual(set(scene), expected_fields)
            self.assertLessEqual(set(scene), ALLOWED_SCENE_FIELDS)
            self.assertLessEqual(len(scene["props"]), 3)
            occupancy = scene["framing"].get("fin_occupancy")
            if occupancy is not None:
                self.assertGreaterEqual(occupancy, 0)
                self.assertLessEqual(occupancy, 1)
            for forbidden in (
                "provider",
                "model",
                "negative_rules",
                "palette",
                "visual_rules",
                "reference_profile",
            ):
                self.assertNotIn(forbidden, scene)

    def test_character_presence_is_explicit_and_schema_constrained(self) -> None:
        payload = load_visual_scenes()
        scenes = payload["scenes"]
        expected = {
            **{f"S{number:03d}": "FIN" for number in range(1, 10)},
            "S010": "NONE",
            **{f"S{number:03d}": "FIN" for number in range(11, 17)},
            "S017": "NONE",
            "S018": "NONE",
        }
        self.assertEqual(
            {scene["scene_id"]: scene["character_presence"] for scene in scenes},
            expected,
        )

        missing = deepcopy(payload)
        del missing["scenes"][0]["character_presence"]
        self.assertTrue(validate_visual_scenes(missing))

        invalid_value = deepcopy(payload)
        invalid_value["scenes"][0]["character_presence"] = "INFER"
        self.assertTrue(validate_visual_scenes(invalid_value))

        invalid_character = deepcopy(payload)
        invalid_character["scenes"][0]["character_presence"] = "NONE"
        self.assertTrue(validate_visual_scenes(invalid_character))

        invalid_data = deepcopy(payload)
        invalid_data["scenes"][-1]["character_presence"] = "FIN"
        self.assertTrue(validate_visual_scenes(invalid_data))

        allowed_object_and_environment = deepcopy(payload)
        allowed_object_and_environment["scenes"][9]["character_presence"] = "FIN"
        allowed_object_and_environment["scenes"][5]["character_presence"] = "NONE"
        self.assertEqual(validate_visual_scenes(allowed_object_and_environment), [])

    def test_simple_data_scene_uses_structured_data_visual_only(self) -> None:
        payload = load_visual_scenes()
        simple_data = payload["scenes"][-1]
        self.assertEqual(
            simple_data["data_visual"],
            {
                "type": "point_distribution",
                "count": 16,
                "colors": ["#111111", "#C4E538"],
                "distribution": "moderate",
                "point_radius": 14,
            },
        )
        self.assertTrue(
            all(
                "data_visual" not in scene
                for scene in payload["scenes"]
                if scene["scene_type"] != "SIMPLE_DATA_SCENE"
            )
        )

        invalid_variants = []
        missing = deepcopy(payload)
        del missing["scenes"][-1]["data_visual"]
        invalid_variants.append(missing)
        wrong_type = deepcopy(payload)
        wrong_type["scenes"][-1]["data_visual"]["type"] = "bars"
        invalid_variants.append(wrong_type)
        zero_count = deepcopy(payload)
        zero_count["scenes"][-1]["data_visual"]["count"] = 0
        invalid_variants.append(zero_count)
        bad_color = deepcopy(payload)
        bad_color["scenes"][-1]["data_visual"]["colors"] = ["black"]
        invalid_variants.append(bad_color)
        bad_radius = deepcopy(payload)
        bad_radius["scenes"][-1]["data_visual"]["point_radius"] = 0
        invalid_variants.append(bad_radius)
        incompatible_text = deepcopy(payload)
        incompatible_text["scenes"][-1]["text_policy"] = {
            "mode": "OVERLAY",
            "items": [{"text": "X", "target": "chart"}],
        }
        invalid_variants.append(incompatible_text)

        for invalid in invalid_variants:
            with self.subTest(errors=validate_visual_scenes(invalid)):
                self.assertTrue(validate_visual_scenes(invalid))

    def test_generation_jobs_are_raw_json_scenes_without_prompt_artifacts(self) -> None:
        jobs = build_generation_jobs(["S004", "S007", "S009"])

        self.assertEqual([job["scene_id"] for job in jobs], ["S004", "S007", "S009"])
        for job in jobs:
            self.assertNotIn("source_prompt", job)
            self.assertNotIn("single_line_prompt", job)
            self.assertNotIn("compiled_prompt", job)

    def test_text_policy_is_explicit_and_uses_only_production_modes(self) -> None:
        scenes = load_visual_scenes()["scenes"]
        policies = {scene["scene_id"]: scene["text_policy"] for scene in scenes}

        self.assertEqual(TEXT_POLICY_MODES, ("NONE", "OVERLAY"))
        self.assertEqual(policies["S010"]["mode"], "OVERLAY")
        self.assertEqual(
            [item["text"] for item in policies["S010"]["items"]],
            ["PESQUISA", "MÊS 1", "MÊS 2", "MÊS 3"],
        )
        self.assertTrue(
            all(
                policy["mode"] == "NONE" and policy["items"] == []
                for scene_id, policy in policies.items()
                if scene_id != "S010"
            )
        )

    def test_exact_mode_and_missing_text_policy_are_rejected(self) -> None:
        exact_payload = deepcopy(load_visual_scenes())
        exact_payload["scenes"][0]["text_policy"] = {"mode": "EXACT", "items": []}
        self.assertTrue(
            any(
                "invalid text_policy mode" in error
                for error in validate_visual_scenes(exact_payload)
            )
        )

        missing_payload = deepcopy(load_visual_scenes())
        del missing_payload["scenes"][0]["text_policy"]
        self.assertTrue(
            any(
                "missing fields text_policy" in error
                for error in validate_visual_scenes(missing_payload)
            )
        )


class PromptCompilationTest(unittest.TestCase):
    def test_build_prompt_resolves_locks_and_returns_one_line(self) -> None:
        scene = build_generation_jobs(["S004"])[0]
        prompt = build_prompt(scene)

        self.assertEqual(validate_compiled_prompt(prompt), [])
        self.assertNotIn("\n", prompt)
        self.assertNotIn("\r", prompt)
        self.assertIn("FIN_V1", prompt)
        self.assertIn("ILLUSTRATED_V1", prompt)
        self.assertIn("assets/character_bible/fin_turnaround.png", prompt)
        self.assertIn("NEGATIVE RULES:", prompt)
        self.assertIn("normal daily purchase", prompt)

    def test_source_prompt_exists_only_as_debug_representation(self) -> None:
        scene = build_generation_jobs(["S009"])[0]
        source = build_source_prompt(scene)
        compiled = build_prompt(scene)

        self.assertIn("\n", source)
        self.assertNotIn("\n", compiled)
        self.assertEqual(
            [block.split("\n", 1)[0] for block in source.split("\n\n")],
            [block.split(": ", 1)[0] for block in compiled.split(" | ")],
        )

    def test_object_scene_blocks_all_undeclared_characters(self) -> None:
        prompt = build_prompt(build_generation_jobs(["S010"])[0])

        self.assertIn("NO CHARACTERS. NO PEOPLE. NO FIN.", prompt)
        self.assertNotIn("FIN must feel integrated", prompt)
        self.assertNotIn("FIN normally occupies", prompt)

    def test_character_presence_comes_only_from_explicit_field(self) -> None:
        no_character = deepcopy(build_generation_jobs(["S017"])[0])
        no_character["action"] = "FIN appears only in this narrative debug sentence."
        no_character_prompt = build_prompt(no_character)

        self.assertIn("NO CHARACTERS. NO PEOPLE. NO FIN.", no_character_prompt)
        self.assertNotIn("Canonical character lock: FIN_V1", no_character_prompt)

        with_character = deepcopy(build_generation_jobs(["S004"])[0])
        for field in (
            "dominant_idea",
            "situation",
            "action",
            "expression",
            "environment",
        ):
            with_character[field] = str(with_character[field]).replace("FIN", "the subject")
        with_character["props"] = [
            str(prop).replace("FIN", "the subject") for prop in with_character["props"]
        ]
        with_character_prompt = build_prompt(with_character)

        self.assertIn("Canonical character lock: FIN_V1", with_character_prompt)
        self.assertIn("FIN normally occupies", with_character_prompt)
        self.assertNotIn("NO CHARACTERS. NO PEOPLE. NO FIN.", with_character_prompt)

    def test_visual_behavior_modules_have_no_scene_id_hardcoding(self) -> None:
        prompt_source = inspect.getsource(prompt_builder)
        routing_source = inspect.getsource(visual_routing)

        self.assertNotIn("scene_id", prompt_source)
        self.assertNotIn("scene_id", routing_source)
        self.assertNotIn("S010", prompt_source)
        self.assertNotIn("S015", prompt_source)

    def test_illustrated_style_lock_applies_to_every_scene_type(self) -> None:
        representative_scenes = {
            "CHARACTER_SCENE": "S004",
            "OBJECT_SCENE": "S017",
            "ENVIRONMENT_SCENE": "S006",
            "SIMPLE_DATA_SCENE": "S018",
        }

        for scene_type, scene_id in representative_scenes.items():
            with self.subTest(scene_type=scene_type):
                prompt = build_prompt(build_generation_jobs([scene_id])[0])
                self.assertIn("ILLUSTRATED_V1 STYLE LOCK.", prompt)
                self.assertIn("clean illustrated 2D visual", prompt)
                self.assertIn("same visual universe as FIN scenes", prompt)
                self.assertIn("clean editorial illustration", prompt)
                self.assertIn("NOT PHOTOREALISTIC.", prompt)
                self.assertIn("NO PHOTOGRAPHY.", prompt)
                self.assertIn("NO 3D RENDER.", prompt)
                self.assertEqual(validate_compiled_prompt(prompt), [])

    def test_s017_blocks_photorealism_and_undeclared_character(self) -> None:
        prompt = build_prompt(build_generation_jobs(["S017"])[0])

        self.assertIn("SCENE TYPE: OBJECT_SCENE", prompt)
        self.assertIn("OBJECT_SCENE GUARD.", prompt)
        self.assertIn("No photorealism", prompt)
        self.assertIn("stock-photo aesthetic", prompt)
        self.assertIn("product photography", prompt)
        self.assertIn("cinematic photography", prompt)
        self.assertIn("NO CHARACTERS. NO PEOPLE. NO FIN.", prompt)
        self.assertNotIn("FIN must feel integrated", prompt)
        self.assertNotIn("FIN normally occupies", prompt)
        self.assertNotIn("CHARACTER EMPHASIS RULE:", prompt)
        self.assertNotIn("captured moment from everyday life", prompt)
        self.assertIn("Static composition is allowed", prompt)

    def test_s018_blocks_undeclared_fin_and_added_narrative(self) -> None:
        prompt = build_prompt(build_generation_jobs(["S018"])[0])

        self.assertIn("SCENE TYPE: SIMPLE_DATA_SCENE", prompt)
        self.assertIn("NO CHARACTERS. NO PEOPLE. NO FIN.", prompt)
        self.assertIn("SIMPLE_DATA_SCENE GUARD.", prompt)
        for forbidden_addition in ("celebration", "confetti", "decorative narrative"):
            self.assertIn(forbidden_addition, prompt)
        self.assertNotIn("FIN must feel integrated", prompt)
        self.assertNotIn("FIN normally occupies", prompt)
        self.assertNotIn("captured moment from everyday life", prompt)
        self.assertNotIn("No infographic", prompt)
        self.assertNotIn("abstract metaphor", prompt)
        self.assertNotIn("symbolic composition", prompt)
        self.assertIn("clean editorial data visualization", prompt)
        self.assertIn("No axes or labels unless declared", prompt)

    def test_character_only_rules_are_scoped_to_character_scene(self) -> None:
        character_prompt = build_prompt(build_generation_jobs(["S016"])[0])
        object_prompt = build_prompt(build_generation_jobs(["S017"])[0])
        data_prompt = build_prompt(build_generation_jobs(["S018"])[0])

        for character_rule in (
            "captured moment from everyday life",
            "FIN normally occupies",
            "CHARACTER EMPHASIS RULE:",
            "natural functional pose",
            "Treat the declared ACTION and EXPRESSION as mandatory instructions",
        ):
            self.assertIn(character_rule, character_prompt)
            self.assertNotIn(character_rule, object_prompt)
            self.assertNotIn(character_rule, data_prompt)

    def test_none_policy_forbids_undeclared_text_explicitly(self) -> None:
        prompt = build_prompt(build_generation_jobs(["S009"])[0])

        self.assertIn("TEXT POLICY: NONE.", prompt)
        self.assertIn("Render no visible text, no letters, no numbers", prompt)
        self.assertIn("NO UNDECLARED TEXT.", prompt)
        for forbidden_kind in (
            "No readable text",
            "random numbers",
            "invented labels",
            "pseudo-words",
            "gibberish",
            "floating typography",
        ):
            self.assertIn(forbidden_kind, prompt)
        self.assertNotIn("Declared text must appear exactly as written.", prompt)
        self.assertNotIn("PESQUISA", prompt)
        self.assertNotIn("MÊS 1", prompt)
        self.assertEqual(prompt.count("TEXT POLICY:"), 1)

    def test_s010_overlay_keeps_text_in_metadata_and_out_of_provider_prompt(self) -> None:
        scene = build_generation_jobs(["S010"])[0]
        prompt = build_prompt(scene)

        self.assertEqual(scene["text_policy"]["mode"], "OVERLAY")
        self.assertEqual(
            [item["text"] for item in scene["text_policy"]["items"]],
            ["PESQUISA", "MÊS 1", "MÊS 2", "MÊS 3"],
        )
        self.assertIn("TEXT POLICY: OVERLAY.", prompt)
        for text in ("PESQUISA", "MÊS 1", "MÊS 2", "MÊS 3"):
            self.assertNotIn(text, prompt)
        self.assertIn("structured metadata only", prompt)
        self.assertIn("clean blank surfaces reserved for later deterministic text overlay", prompt)
        self.assertIn(scene["action"], prompt)
        for prop in scene["props"]:
            self.assertIn(prop, prompt)
        self.assertIn("SCENE TYPE: OBJECT_SCENE", prompt)
        self.assertIn("NO CHARACTERS. NO PEOPLE. NO FIN.", prompt)
        self.assertNotIn("\n", prompt)
        self.assertNotIn("\r", prompt)

    def test_overlay_policy_requests_blank_surface_without_rendering_text(self) -> None:
        scene = deepcopy(build_generation_jobs(["S004"])[0])
        scene["text_policy"] = {
            "mode": "OVERLAY",
            "items": [{"text": "R$ 4.200", "target": "phone screen"}],
        }
        prompt = build_prompt(scene)

        self.assertIn("TEXT POLICY: OVERLAY.", prompt)
        self.assertIn(
            "clean blank surfaces reserved for later deterministic text overlay on phone screen",
            prompt,
        )
        self.assertIn("Do not render their content.", prompt)
        self.assertIn("NO UNDECLARED TEXT.", prompt)
        self.assertNotIn("Declared text must appear exactly as written.", prompt)
        self.assertNotIn("R$ 4.200", prompt)
        self.assertNotIn("\n", prompt)
        self.assertNotIn("\r", prompt)

    def test_action_expression_and_declared_props_remain_mandatory(self) -> None:
        scene = build_generation_jobs(["S016"])[0]
        prompt = build_prompt(scene)

        self.assertIn(scene["action"], prompt)
        self.assertIn(scene["expression"], prompt)
        for prop in scene["props"]:
            self.assertIn(prop, prompt)
        self.assertIn("Treat the declared ACTION and EXPRESSION as mandatory instructions", prompt)
        self.assertIn("Include every declared prop and do not remove essential props", prompt)

    def test_s015_intent_is_fully_declared_in_generic_scene_fields(self) -> None:
        scene = build_generation_jobs(["S015"])[0]
        prompt = build_prompt(scene)
        declared_content = " ".join(
            [scene["action"], scene["expression"], *scene["props"]]
        ).lower()

        for expected in (
            "groceries",
            "quiet relief",
            "medicine box",
            "bill envelope",
        ):
            self.assertIn(expected, declared_content)
        self.assertIn(scene["action"], prompt)
        self.assertIn(scene["expression"], prompt)
        for prop in scene["props"]:
            self.assertIn(prop, prompt)

    def test_model_autonomy_is_limited_to_appearance(self) -> None:
        prompt = build_prompt(build_generation_jobs(["S004"])[0])

        self.assertIn("SYSTEM DECIDES CONTENT. AI DECIDES APPEARANCE.", prompt)
        self.assertIn("Do not introduce undeclared narrative elements", prompt)
        self.assertIn("The model decides only their fine visual rendering", prompt)


class UpgradeProvider:
    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(PNG_BYTES)
        return GenerationResult(
            success=True,
            image_path=request.output_path,
            cost_usd=0.05,
            request_id="upgrade-request-1",
        )


class DeterministicRendererTest(unittest.TestCase):
    def test_same_structured_data_ignores_identifier_and_prose(self) -> None:
        scene = deepcopy(build_generation_jobs(["S018"])[0])
        prose_variant = deepcopy(scene)
        prose_variant["scene_id"] = "FUTURE-DATA-SCENE"
        prose_variant["action"] = "Completely unrelated prose."
        prose_variant["props"] = ["No semantic renderer instructions here."]

        with tempfile.TemporaryDirectory(prefix="deterministic-repeat-") as temporary:
            first = Path(temporary) / "first.png"
            second = Path(temporary) / "second.png"
            render_deterministic_scene(scene, first)
            render_deterministic_scene(prose_variant, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
        source = inspect.getsource(deterministic_renderer)
        self.assertNotIn("scene_id", source)
        self.assertNotIn('scene.get("action"', source)
        self.assertNotIn('scene.get("props"', source)

    def test_count_changes_rendered_point_quantity(self) -> None:
        small = deepcopy(build_generation_jobs(["S018"])[0])
        large = deepcopy(small)
        small["data_visual"]["count"] = 4
        small["data_visual"]["point_radius"] = 5
        large["data_visual"]["count"] = 9
        large["data_visual"]["point_radius"] = 5
        radius = 5
        pixels_per_point = sum(
            x * x + y * y <= radius * radius
            for y in range(-radius, radius + 1)
            for x in range(-radius, radius + 1)
        )

        with tempfile.TemporaryDirectory(prefix="deterministic-count-") as temporary:
            small_path = Path(temporary) / "small.png"
            large_path = Path(temporary) / "large.png"
            render_deterministic_scene(small, small_path)
            render_deterministic_scene(large, large_path)
            _, small_colors = _png_rgb_counts(small_path)
            _, large_colors = _png_rgb_counts(large_path)

        background = official_background_rgb()
        small_foreground = sum(
            count for color, count in small_colors.items() if color != background
        )
        large_foreground = sum(
            count for color, count in large_colors.items() if color != background
        )
        self.assertEqual(small_foreground, 4 * pixels_per_point)
        self.assertEqual(large_foreground, 9 * pixels_per_point)

    def test_declared_colors_are_the_only_point_colors_used(self) -> None:
        scene = deepcopy(build_generation_jobs(["S018"])[0])
        scene["data_visual"]["count"] = 6
        scene["data_visual"]["colors"] = ["#123456", "#ABCDEF", "#FF5500"]
        declared = {
            tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
            for color in scene["data_visual"]["colors"]
        }

        with tempfile.TemporaryDirectory(prefix="deterministic-colors-") as temporary:
            output = Path(temporary) / "colors.png"
            render_deterministic_scene(scene, output)
            _, colors = _png_rgb_counts(output)

        self.assertEqual(set(colors), {official_background_rgb(), *declared})


class VisualGenerationPipelineTest(unittest.TestCase):
    def test_config_and_reference_profile_are_valid(self) -> None:
        config = load_generation_config()
        profile = load_reference_profile()

        self.assertEqual(validate_generation_config(config), [])
        self.assertEqual(validate_reference_profile(profile, ROOT), [])
        self.assertTrue(config["resume"])
        self.assertFalse(config["tiers"]["premium_optional"]["enabled"])
        self.assertEqual(config["default_provider"], "openrouter")
        self.assertEqual(config["default_budget_usd"], 0.15)
        self.assertTrue(config["quality_first"])
        self.assertFalse(config["auto_quality_upgrade"])
        self.assertEqual(
            config["generation_principle"],
            "USE GENERATIVE AI ONLY WHEN GENERATIVE AI ADDS VALUE.",
        )
        self.assertEqual(config["scene_type_routing"]["source"], "SCENE_TYPE")
        self.assertEqual(
            config["decision_priority"],
            [
                "narrative_adherence",
                "FIN_V1_consistency",
                "visual_quality",
                "continuity",
                "cost",
            ],
        )
        self.assertEqual(
            config["tiers"]["cheap_draft"]["model"],
            "black-forest-labs/flux.2-klein-4b",
        )
        self.assertEqual(
            config["tiers"]["mid_fallback"]["model"], "openai/gpt-image-2"
        )
        self.assertEqual(config["tiers"]["cheap_draft"]["timeout"], 120)
        self.assertEqual(config["tiers"]["mid_fallback"]["timeout"], 180)
        self.assertEqual(config["tiers"]["premium_optional"]["timeout"], 180)

    def test_dry_run_routes_each_scene_type_without_api_or_images(self) -> None:
        scene_ids = [f"S{number:03d}" for number in range(10, 16)]
        with tempfile.TemporaryDirectory(prefix="visual-first-batch-dry-") as temporary:
            output = Path(temporary)
            manifest = run_generation(scene_ids, output_dir=output, dry_run=True)

            self.assertEqual([scene["scene_id"] for scene in manifest["scenes"]], scene_ids)
            self.assertTrue(all(scene["status"] == "dry_run" for scene in manifest["scenes"]))
            by_id = {scene["scene_id"]: scene for scene in manifest["scenes"]}
            self.assertEqual(by_id["S010"]["selected_tier"], "mid_fallback")
            self.assertEqual(by_id["S010"]["model"], "openai/gpt-image-2")
            for scene_id in ("S011", "S012", "S013", "S014", "S015"):
                self.assertEqual(by_id[scene_id]["selected_tier"], "cheap_draft")
                self.assertEqual(
                    by_id[scene_id]["model"],
                    "black-forest-labs/flux.2-klein-4b",
                )
            self.assertTrue(all(scene["review_status"] == "PENDING" for scene in manifest["scenes"]))
            self.assertEqual(list(output.rglob("*.png")), [])
            self.assertFalse((output / "request_audit.jsonl").exists())
            self.assertTrue((output / "review_index.html").is_file())
            self.assertEqual(validate_output(output, ROOT), [])

    def test_dry_run_all_uses_json_and_creates_no_prompt_txt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-json-dry-") as temporary:
            output = Path(temporary)
            manifest = run_generation(None, output_dir=output, dry_run=True)

            self.assertEqual(len(manifest["scenes"]), 18)
            self.assertTrue(all(scene["status"] == "dry_run" for scene in manifest["scenes"]))
            self.assertEqual(manifest["budget"]["spent_usd"], 0.0)
            self.assertEqual(list(output.rglob("*.txt")), [])
            self.assertEqual(list(output.glob("S*.png")), [])
            self.assertEqual(validate_output(output, ROOT), [])

    def test_mock_provider_writes_draft_output_and_canonical_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-json-mock-") as temporary:
            output = Path(temporary)
            manifest = run_generation(
                ["S004", "S009"],
                output_dir=output,
                resume=False,
                provider_registry=ProviderRegistry(
                    {"openrouter": MockVisualProvider()}
                ),
            )

            self.assertTrue((output / "drafts" / "S004.png").is_file())
            self.assertTrue((output / "drafts" / "S009.png").is_file())
            self.assertFalse((output / "S004").exists())
            self.assertEqual(validate_output(output, ROOT), [])
            for scene in manifest["scenes"]:
                self.assertEqual(set(scene), SCENE_MANIFEST_FIELDS)
                self.assertEqual(scene["status"], "generated")
                self.assertEqual(scene["selected_tier"], "cheap_draft")
                self.assertEqual(scene["review_status"], "PENDING")
                self.assertEqual(manifest["fin_lock"], "FIN_V1")
                self.assertEqual(manifest["visual_profile"], "ILLUSTRATED_V1")
                self.assertEqual(len(scene["prompt_hash"]), 64)

    def test_fallback_from_cheap_to_mid_still_works(self) -> None:
        registry = ProviderRegistry(
            {"openrouter": MockVisualProvider(fail_tiers={"cheap_draft"})}
        )
        with tempfile.TemporaryDirectory(prefix="visual-json-fallback-") as temporary:
            manifest = run_generation(
                ["S004"],
                output_dir=Path(temporary),
                budget_usd=1.0,
                resume=False,
                provider_registry=registry,
            )

            scene = manifest["scenes"][0]
            self.assertEqual(scene["status"], "generated")
            self.assertEqual(scene["selected_tier"], "mid_fallback")
            self.assertEqual(scene["attempts"], 2)
            self.assertAlmostEqual(scene["cost_usd"], 0.1)

    def test_routing_uses_scene_type_and_character_presence_not_prose_or_scene_id(self) -> None:
        config = load_generation_config()
        character = deepcopy(build_generation_jobs(["S016"])[0])
        object_scene = deepcopy(build_generation_jobs(["S017"])[0])
        data_scene = deepcopy(build_generation_jobs(["S018"])[0])
        character["scene_id"] = "FUTURE-CHARACTER"
        object_scene["scene_id"] = "FUTURE-OBJECT"
        data_scene["scene_id"] = "FUTURE-DATA"

        character_route = resolve_visual_route(character, config)
        object_route = resolve_visual_route(object_scene, config)
        data_route = resolve_visual_route(data_scene, config)

        self.assertEqual(character_route.model, "black-forest-labs/flux.2-klein-4b")
        self.assertEqual(character_route.primary_tier, "cheap_draft")
        self.assertEqual(object_route.model, "openai/gpt-image-2")
        self.assertEqual(object_route.primary_tier, "mid_fallback")
        self.assertFalse(object_route.allow_fallback)
        self.assertEqual(data_route.renderer, "deterministic_local")
        self.assertEqual(data_route.provider, "local")
        self.assertFalse(data_route.api_required)
        self.assertEqual(data_route.cost_usd, 0.0)

        object_scene["action"] = "FIN appears only in this narrative debug sentence."
        object_with_fin_in_prose_route = resolve_visual_route(object_scene, config)
        self.assertEqual(object_with_fin_in_prose_route.model, "openai/gpt-image-2")
        self.assertEqual(object_with_fin_in_prose_route.primary_tier, "mid_fallback")

        object_scene["action"] = "The declared object remains on the desk."
        object_scene["character_presence"] = "FIN"
        object_with_fin_route = resolve_visual_route(object_scene, config)
        self.assertEqual(
            object_with_fin_route.model,
            "black-forest-labs/flux.2-klein-4b",
        )
        self.assertEqual(object_with_fin_route.primary_tier, "cheap_draft")

    def test_simple_data_scene_renders_locally_with_zero_api_and_zero_cost(self) -> None:
        class ApiMustNotBeCalled:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, request):
                self.calls += 1
                raise AssertionError("generative provider must not be called")

        provider = ApiMustNotBeCalled()
        with tempfile.TemporaryDirectory(prefix="visual-local-data-") as temporary:
            output = Path(temporary)
            manifest = run_generation(
                ["S018"],
                output_dir=output,
                budget_usd=0,
                resume=False,
                provider_registry=ProviderRegistry({"openrouter": provider}),
            )

            scene = manifest["scenes"][0]
            image_path = Path(scene["draft_path"])
            self.assertEqual(provider.calls, 0)
            self.assertEqual(scene["status"], "generated")
            self.assertEqual(scene["review_status"], "PENDING")
            self.assertEqual(scene["selected_tier"], "deterministic_local")
            self.assertEqual(scene["provider"], "local")
            self.assertEqual(scene["model"], "deterministic_local")
            self.assertEqual(scene["cost_usd"], 0.0)
            self.assertEqual(scene["references_used"], [])
            self.assertEqual(manifest["budget"]["spent_usd"], 0.0)
            self.assertFalse((output / "request_audit.jsonl").exists())
            self.assertTrue(image_path.is_file())
            self.assertEqual(validate_output(output, ROOT), [])

            dimensions, colors = _png_rgb_counts(image_path)
            declared_colors = {
                tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
                for color in build_generation_jobs(["S018"])[0]["data_visual"]["colors"]
            }
            self.assertEqual(dimensions, CANVAS_SIZE)
            self.assertEqual(
                set(colors),
                {official_background_rgb(), *declared_colors},
            )

    def test_timeout_after_post_is_unknown_billed_without_retry_and_preserves_draft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-billed-timeout-") as temporary:
            output = Path(temporary)
            first = run_generation(
                ["S004"],
                output_dir=output,
                resume=False,
                provider_registry=ProviderRegistry({"openrouter": MockVisualProvider()}),
            )
            previous_draft = first["scenes"][0]["draft_path"]
            draft_path = ROOT / previous_draft
            previous_bytes = draft_path.read_bytes()
            observed_timeouts: list[int] = []

            def timeout_transport(request, *, timeout):
                observed_timeouts.append(timeout)
                raise TimeoutError("mock image generation timeout")

            timeout_provider = OpenRouterImageProvider(
                api_key="test-key",
                root=ROOT,
                transport=timeout_transport,
            )
            timed_out = run_generation(
                ["S004"],
                output_dir=output,
                resume=False,
                provider_registry=ProviderRegistry({"openrouter": timeout_provider}),
                allow_rerun=True,
                rerun_reason="mock billed timeout",
            )

            scene = timed_out["scenes"][0]
            self.assertEqual(observed_timeouts, [120])
            self.assertEqual(scene["status"], UNKNOWN_BILLED_TIMEOUT)
            self.assertEqual(scene["review_status"], "PENDING")
            self.assertEqual(scene["error"], "TimeoutError")
            self.assertIsNone(scene["output_path"])
            self.assertEqual(scene["draft_path"], previous_draft)
            self.assertEqual(draft_path.read_bytes(), previous_bytes)
            self.assertEqual(scene["attempts"], 1)
            self.assertEqual(scene["selected_tier"], "cheap_draft")
            self.assertAlmostEqual(scene["cost_usd"], 0.02)

            audit = [
                json.loads(line)
                for line in (output / "request_audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([entry["event"] for entry in audit], ["request", "response"])
            self.assertEqual({entry["request_sequence"] for entry in audit}, {1})
            self.assertEqual({entry["scene_id"] for entry in audit}, {"S004"})
            self.assertEqual(
                {entry["model"] for entry in audit},
                {"black-forest-labs/flux.2-klein-4b"},
            )
            self.assertEqual(audit[-1]["status"], UNKNOWN_BILLED_TIMEOUT)
            self.assertIsNone(audit[-1]["request_id"])
            self.assertEqual(validate_output(output, ROOT), [])

            approved = approve_scenes(["S004"], output_dir=output)
            approved_scene = approved["scenes"][0]
            self.assertEqual(approved_scene["status"], UNKNOWN_BILLED_TIMEOUT)
            self.assertEqual(approved_scene["review_status"], "APPROVED")
            self.assertEqual(approved_scene["final_path"], previous_draft)
            self.assertEqual(draft_path.read_bytes(), previous_bytes)
            self.assertEqual(len(observed_timeouts), 1)
            self.assertEqual(validate_output(output, ROOT), [])

    def test_budget_manager_stops_before_overspend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-json-budget-") as temporary:
            manifest = run_generation(
                ["S004"],
                output_dir=Path(temporary),
                budget_usd=0.01,
                resume=False,
            )

            scene = manifest["scenes"][0]
            self.assertEqual(scene["status"], "skipped_budget")
            self.assertEqual(scene["attempts"], 0)
            self.assertEqual(scene["cost_usd"], 0.0)
            self.assertEqual(manifest["budget"]["spent_usd"], 0.0)

    def test_resume_reuses_matching_prompt_hash_and_existing_image(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-json-resume-") as temporary:
            output = Path(temporary)
            registry = ProviderRegistry({"openrouter": MockVisualProvider()})
            first = run_generation(
                ["S004"],
                output_dir=output,
                resume=False,
                provider_registry=registry,
            )
            second = run_generation(
                ["S004"],
                output_dir=output,
                resume=True,
                provider_registry=registry,
                allow_rerun=True,
                rerun_reason="resume verification",
            )

            self.assertEqual(first["scenes"][0], second["scenes"][0])
            self.assertEqual(second["resumed_count"], 1)
            self.assertEqual(second["budget"]["spent_usd"], 0.0)

    def test_approve_preserves_draft_and_does_not_generate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-approve-") as temporary:
            output = Path(temporary)
            registry = ProviderRegistry({"openrouter": MockVisualProvider()})
            run_generation(
                ["S010"],
                output_dir=output,
                resume=False,
                provider_registry=registry,
            )
            draft = output / "drafts" / "S010.png"
            manifest = approve_scenes(["S010"], output_dir=output)

            scene = manifest["scenes"][0]
            self.assertEqual(scene["review_status"], "APPROVED")
            self.assertEqual(scene["final_path"], scene["draft_path"])
            self.assertTrue(draft.is_file())
            self.assertEqual(validate_output(output, ROOT), [])

    def test_upgrade_uses_gpt_directly_and_preserves_flux_draft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-upgrade-") as temporary:
            output = Path(temporary)
            registry = ProviderRegistry({"openrouter": MockVisualProvider()})
            run_generation(
                ["S012"],
                output_dir=output,
                resume=False,
                provider_registry=registry,
            )
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["scenes"][0]["review_status"] = "UPGRADE_REQUESTED"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            provider = UpgradeProvider()

            upgraded = upgrade_scenes(
                ["S012"],
                output_dir=output,
                provider_registry=ProviderRegistry({"openrouter": provider}),
            )

            scene = upgraded["scenes"][0]
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(provider.requests[0].model, "openai/gpt-image-2")
            self.assertEqual(provider.requests[0].tier, "mid_fallback")
            self.assertEqual(provider.requests[0].timeout, 180)
            self.assertEqual(scene["review_status"], "PENDING")
            self.assertTrue((output / "drafts" / "S012.png").is_file())
            self.assertTrue((output / "final" / "S012.png").is_file())
            self.assertNotEqual(scene["draft_path"], scene["final_path"])
            self.assertEqual(validate_output(output, ROOT), [])

    def test_completed_real_run_requires_explicit_rerun_when_resume_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-completed-") as temporary:
            output = Path(temporary)
            registry = ProviderRegistry({"openrouter": MockVisualProvider()})
            run_generation(
                ["S010"],
                output_dir=output,
                resume=False,
                provider_registry=registry,
            )
            with self.assertRaisesRegex(RuntimeError, "rerun requires explicit authorization"):
                run_generation(
                    ["S010"],
                    output_dir=output,
                    resume=True,
                    provider_registry=registry,
                )


class PreservedEditorialDataTest(unittest.TestCase):
    def test_scene_map_is_a_valid_dynamic_episode_subset(self) -> None:
        episode = resolve_active_episode(ROOT)
        visual_rows = load_visual_script(episode.file("roteiro_visual.csv"))
        planned_ids = [row["scene_id"] for row in visual_rows]
        planned_types = {row["scene_id"]: row["scene_type"] for row in visual_rows}
        scene_map = load_episode_json(episode.file("scene_map.json"))

        self.assertEqual(
            validate_scene_map(
                scene_map,
                expected_episode_id=episode.episode_id,
                planned_scene_ids=planned_ids,
                planned_scene_types=planned_types,
                production_stage=episode.production_stage,
                expected_timing_quality=str(episode.project["timing_quality"]),
            ),
            [],
        )
        self.assertEqual(scene_map["timing_quality"], "WORD_BOUNDARY_REAL")

    def test_visual_script_is_a_valid_dynamic_static_sequence_without_text(self) -> None:
        episode = resolve_active_episode(ROOT)
        rows = load_visual_script(episode.file("roteiro_visual.csv"))

        self.assertEqual(validate_visual_script(rows), [])
        self.assertTrue(rows)
        self.assertEqual(
            len([row["scene_id"] for row in rows]),
            len({row["scene_id"] for row in rows}),
        )
        self.assertTrue(all(row["format"] == "image" for row in rows))
        self.assertTrue(all(not row["text_on_screen"].strip() for row in rows))

    def test_fin_v1_is_unchanged_and_canonical(self) -> None:
        lock = json.loads((ROOT / "config" / "character_fin.json").read_text(encoding="utf-8"))

        self.assertEqual(lock["character_lock_version"], "FIN_V1")
        self.assertEqual(
            lock["canonical_references"],
            [
                "assets/character_bible/fin_turnaround.png",
                "assets/character_bible/fin_poses.png",
            ],
        )


if __name__ == "__main__":
    unittest.main()

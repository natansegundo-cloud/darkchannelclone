from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import tempfile
import unittest

import src.visuals.benchmark as benchmark_module
import src.visuals.generation_runner as generation_module
import src.visuals.references as references_module
from src.visuals.benchmark import load_benchmark_config, run_benchmark
from src.visuals.engine import build_generation_jobs, load_reference_profile
from src.visuals.generation_runner import run_generation, upgrade_scenes
from src.visuals.providers import GenerationResult, ProviderRegistry
from src.visuals.references import resolve_scene_references


FIN_REFERENCES = [
    "assets/character_bible/fin_turnaround.png",
    "assets/character_bible/fin_poses.png",
]


class NeverCallProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise AssertionError("dry-run must not call a provider")


class CaptureFailureProvider:
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return GenerationResult(success=False, error="expected test failure")


class ConditionalVisualReferenceTest(unittest.TestCase):
    def test_character_scene_with_fin_receives_two_fin_references(self) -> None:
        scene = build_generation_jobs(["S012"])[0]
        profile = load_reference_profile()

        self.assertEqual(resolve_scene_references(scene, profile), FIN_REFERENCES)

    def test_object_scene_without_fin_receives_no_fin_references(self) -> None:
        scene = build_generation_jobs(["S017"])[0]
        profile = load_reference_profile()

        self.assertEqual(resolve_scene_references(scene, profile), [])

    def test_object_scene_with_fin_receives_fin_references(self) -> None:
        scene = deepcopy(build_generation_jobs(["S017"])[0])
        scene["character_presence"] = "FIN"
        profile = load_reference_profile()

        self.assertEqual(resolve_scene_references(scene, profile), FIN_REFERENCES)

    def test_none_scene_reference_requires_explicit_none_compatibility(self) -> None:
        scene = build_generation_jobs(["S017"])[0]
        profile = deepcopy(load_reference_profile())
        profile["scene_reference_images"]["S017"] = "assets/visual_references/S004.png"

        with self.assertRaisesRegex(ValueError, "explicit NONE compatibility"):
            resolve_scene_references(scene, profile)

        profile["scene_reference_character_presence"] = {"S017": "NONE"}
        self.assertEqual(
            resolve_scene_references(scene, profile),
            ["assets/visual_references/S004.png"],
        )

    def test_s017_normal_generation_manifest_has_zero_fin_references(self) -> None:
        provider = NeverCallProvider()
        with tempfile.TemporaryDirectory(prefix="s017-reference-dry-") as temporary:
            output = Path(temporary)
            manifest = run_generation(
                ["S017"],
                output_dir=output,
                dry_run=True,
                resume=False,
                provider_registry=ProviderRegistry({"openrouter": provider}),
            )

            self.assertEqual(provider.calls, 0)
            self.assertEqual(manifest["scenes"][0]["references_used"], [])
            self.assertEqual(list(output.rglob("*.png")), [])

    def test_upgrade_s017_uses_zero_fin_references(self) -> None:
        dry_provider = NeverCallProvider()
        upgrade_provider = CaptureFailureProvider()
        with tempfile.TemporaryDirectory(prefix="s017-upgrade-reference-") as temporary:
            output = Path(temporary)
            run_generation(
                ["S017"],
                output_dir=output,
                dry_run=True,
                resume=False,
                provider_registry=ProviderRegistry({"openrouter": dry_provider}),
            )
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["scenes"][0]["review_status"] = "UPGRADE_REQUESTED"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            upgraded = upgrade_scenes(
                ["S017"],
                output_dir=output,
                provider_registry=ProviderRegistry(
                    {"openrouter": upgrade_provider}
                ),
            )

            self.assertEqual(dry_provider.calls, 0)
            self.assertEqual(len(upgrade_provider.requests), 1)
            self.assertEqual(upgrade_provider.requests[0].reference_images, ())
            self.assertEqual(upgraded["scenes"][0]["references_used"], [])
            self.assertEqual(list(output.rglob("*.png")), [])

    def test_benchmark_and_normal_generation_share_reference_resolution(self) -> None:
        self.assertIs(
            generation_module.resolve_scene_references,
            references_module.resolve_scene_references,
        )
        self.assertIs(
            benchmark_module.resolve_scene_references,
            references_module.resolve_scene_references,
        )
        self.assertNotIn("required_references", inspect.getsource(generation_module))
        self.assertNotIn("required_references", inspect.getsource(benchmark_module))

        normal_provider = NeverCallProvider()
        benchmark_provider = NeverCallProvider()
        config = deepcopy(load_benchmark_config())
        config["include_scene_reference"] = True
        with tempfile.TemporaryDirectory(prefix="shared-reference-policy-") as temporary:
            root = Path(temporary)
            normal = run_generation(
                ["S004"],
                output_dir=root / "normal",
                dry_run=True,
                resume=False,
                provider_registry=ProviderRegistry(
                    {"openrouter": normal_provider}
                ),
            )
            benchmark = run_benchmark(
                scene_id="S004",
                dry_run=True,
                output_dir=root / "benchmark",
                config=config,
                provider=benchmark_provider,
            )

            expected = normal["scenes"][0]["references_used"]
            self.assertTrue(
                all(result["references_used"] == expected for result in benchmark["results"])
            )
            self.assertEqual(normal_provider.calls, 0)
            self.assertEqual(benchmark_provider.calls, 0)
            self.assertEqual(list(root.rglob("*.png")), [])


if __name__ == "__main__":
    unittest.main()

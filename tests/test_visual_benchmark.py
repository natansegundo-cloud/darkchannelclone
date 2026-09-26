from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from src.visuals.benchmark import (
    MAX_BENCHMARK_COST_USD,
    api_key_configured,
    configure_api_key_environment,
    load_benchmark_config,
    run_benchmark,
    validate_benchmark_config,
)
from src.visuals.providers import (
    GenerationRequest,
    GenerationResult,
    OpenRouterImageProvider,
)


ROOT = Path(__file__).resolve().parents[1]
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.headers = {"x-request-id": "header-request-id"}

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class OpenRouterProviderTest(unittest.TestCase):
    def test_request_auth_references_decode_save_and_usage_cost(self) -> None:
        captured: dict[str, object] = {}

        def transport(request: object, *, timeout: int) -> FakeHTTPResponse:
            captured["calls"] = int(captured.get("calls", 0)) + 1
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                {
                    "id": "response-request-id",
                    "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}],
                    "usage": {"cost": 0.0123},
                }
            )

        with tempfile.TemporaryDirectory(prefix="openrouter-provider-") as temporary:
            root = Path(temporary)
            (root / "first.png").write_bytes(PNG_BYTES)
            (root / "second.png").write_bytes(PNG_BYTES)
            output = root / "result.png"
            audit_log = root / "request_audit.jsonl"
            provider = OpenRouterImageProvider(
                api_key="test-secret-key",
                root=root,
                transport=transport,
            )
            result = provider.generate(
                GenerationRequest(
                    scene_id="S004",
                    prompt="compiled prompt",
                    visual_profile="ILLUSTRATED_V1",
                    fin_lock="FIN_V1",
                    reference_images=("first.png", "second.png"),
                    provider="openrouter",
                    model="openai/gpt-image-2",
                    tier="benchmark",
                    timeout=77,
                    output_path=output,
                    request_sequence=1,
                    request_attempt=1,
                    reason="benchmark_generation",
                    audit_log_path=audit_log,
                    run_id="test-run",
                )
            )

            self.assertTrue(result.success)
            self.assertEqual(result.cost_usd, 0.0123)
            self.assertEqual(result.request_id, "response-request-id")
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertEqual(captured["calls"], 1)
            request = captured["request"]
            self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/images")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-key")
            self.assertEqual(captured["timeout"], 77)
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["model"], "openai/gpt-image-2")
            self.assertEqual(payload["aspect_ratio"], "16:9")
            self.assertEqual(payload["resolution"], "1K")
            self.assertEqual(payload["n"], 1)
            self.assertEqual(len(payload["input_references"]), 2)
            self.assertTrue(
                all(
                    reference["image_url"]["url"].startswith("data:image/png;base64,")
                    for reference in payload["input_references"]
                )
            )
            audit = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["event"] for item in audit], ["request", "response"])
            self.assertEqual(audit[0]["request_sequence"], 1)
            self.assertEqual(audit[0]["scene_id"], "S004")
            self.assertEqual(audit[0]["model"], "openai/gpt-image-2")
            self.assertEqual(audit[0]["request_attempt"], 1)
            self.assertEqual(audit[0]["reason"], "benchmark_generation")
            self.assertEqual(audit[0]["HTTP_method"], "POST")
            self.assertEqual(audit[0]["endpoint"], "https://openrouter.ai/api/v1/images")
            self.assertEqual(audit[1]["usage_cost"], 0.0123)
            self.assertEqual(audit[1]["request_id"], "response-request-id")

    def test_missing_environment_key_fails_without_exposing_a_secret(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = OpenRouterImageProvider()
            result = provider.generate(
                GenerationRequest(
                    scene_id="S004",
                    prompt="prompt",
                    visual_profile="ILLUSTRATED_V1",
                    fin_lock="FIN_V1",
                    reference_images=(),
                    provider="openrouter",
                    model="openai/gpt-image-2",
                    tier="benchmark",
                    timeout=10,
                    output_path=Path("unused.png"),
                )
            )

        self.assertFalse(result.success)
        self.assertIn("OPENROUTER_API_KEY", result.error or "")
        self.assertNotIn("Bearer", result.error or "")

    def test_api_key_status_and_local_env_bootstrap_do_not_expose_value(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openrouter-env-") as temporary:
            env_file = Path(temporary) / ".env"
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "configured"}, clear=True):
                self.assertTrue(api_key_configured(env_file))
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(api_key_configured(env_file))
                env_file.write_text("OPENROUTER_API_KEY=local-secret\n", encoding="utf-8")
                self.assertTrue(api_key_configured(env_file))
                self.assertTrue(configure_api_key_environment(env_file))
                self.assertEqual(os.environ["OPENROUTER_API_KEY"], "local-secret")


class NeverCallProvider:
    def generate(self, _request: GenerationRequest) -> GenerationResult:
        raise AssertionError("provider must not be called during dry-run")


class CostSequenceProvider:
    def __init__(self, costs: list[float]) -> None:
        self.costs = costs
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        request.output_path.write_bytes(PNG_BYTES)
        return GenerationResult(
            success=True,
            image_path=request.output_path,
            cost_usd=self.costs[len(self.requests) - 1],
            request_id=f"request-{len(self.requests)}",
        )


class IsolatedResultProvider:
    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            return GenerationResult(success=False, error="candidate-specific failure")
        request.output_path.write_bytes(PNG_BYTES)
        return GenerationResult(success=True, image_path=request.output_path, cost_usd=0.01)


class VisualBenchmarkTest(unittest.TestCase):
    def test_benchmark_config_has_exact_candidates_and_cap(self) -> None:
        config = load_benchmark_config()

        self.assertEqual(validate_benchmark_config(config), [])
        self.assertEqual(float(MAX_BENCHMARK_COST_USD), 0.15)
        self.assertEqual(
            [candidate["model"] for candidate in config["candidates"]],
            [
                "sourceful/riverflow-v2.5-fast:free",
                "openai/gpt-image-2",
                "black-forest-labs/flux.2-klein-4b",
                "bytedance-seed/seedream-4.5",
            ],
        )

    def test_dry_run_does_not_call_api_and_lists_outputs_and_references(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openrouter-benchmark-dry-") as temporary:
            output = Path(temporary)
            manifest = run_benchmark(
                scene_id="S004",
                dry_run=True,
                output_dir=output,
                provider=NeverCallProvider(),
            )

            self.assertTrue((output / "benchmark.json").is_file())
            self.assertEqual(len(manifest["results"]), 4)
            self.assertTrue(all(item["status"] == "dry_run" for item in manifest["results"]))
            self.assertEqual(list(output.glob("*.png")), [])
            for item in manifest["results"]:
                self.assertEqual(
                    item["references_used"],
                    [
                        "assets/character_bible/fin_turnaround.png",
                        "assets/character_bible/fin_poses.png",
                    ],
                )
                self.assertTrue(item["output_path"].endswith(".png"))

    def test_budget_cap_stops_new_model_calls(self) -> None:
        provider = CostSequenceProvider([0.08, 0.07])
        with tempfile.TemporaryDirectory(prefix="openrouter-benchmark-budget-") as temporary:
            manifest = run_benchmark(
                scene_id="S004",
                dry_run=False,
                output_dir=Path(temporary),
                provider=provider,
            )

            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(manifest["total_cost_usd"], 0.15)
            self.assertEqual(
                [item["request_id"] for item in manifest["results"][:2]],
                ["request-1", "request-2"],
            )
            self.assertEqual(
                [item["status"] for item in manifest["results"]],
                ["generated", "generated", "skipped_budget", "skipped_budget"],
            )

    def test_model_error_is_isolated_without_fallback_substitution(self) -> None:
        provider = IsolatedResultProvider()
        with tempfile.TemporaryDirectory(prefix="openrouter-benchmark-isolated-") as temporary:
            manifest = run_benchmark(
                scene_id="S004",
                dry_run=False,
                output_dir=Path(temporary),
                provider=provider,
            )

            expected_models = [
                "sourceful/riverflow-v2.5-fast:free",
                "openai/gpt-image-2",
                "black-forest-labs/flux.2-klein-4b",
                "bytedance-seed/seedream-4.5",
            ]
            self.assertEqual([request.model for request in provider.requests], expected_models)
            self.assertEqual(Counter(request.model for request in provider.requests), Counter(expected_models))
            self.assertEqual([request.request_sequence for request in provider.requests], [1, 2, 3, 4])
            self.assertTrue(all(request.request_attempt == 1 for request in provider.requests))
            self.assertTrue(all(request.reason == "benchmark_generation" for request in provider.requests))
            self.assertEqual(manifest["results"][0]["status"], "failed")
            self.assertEqual(manifest["results"][0]["error"], "candidate-specific failure")
            self.assertTrue(
                all(item["status"] == "generated" for item in manifest["results"][1:])
            )

    def test_existing_real_benchmark_lock_blocks_every_provider_call(self) -> None:
        provider = IsolatedResultProvider()
        with tempfile.TemporaryDirectory(prefix="openrouter-benchmark-lock-") as temporary:
            output = Path(temporary)
            (output / "benchmark.lock").write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "benchmark lock already exists"):
                run_benchmark(
                    scene_id="S004",
                    dry_run=False,
                    output_dir=output,
                    provider=provider,
                )

            self.assertEqual(provider.requests, [])

    def test_completed_real_round_blocks_unintentional_second_round(self) -> None:
        provider = IsolatedResultProvider()
        with tempfile.TemporaryDirectory(prefix="openrouter-benchmark-rerun-") as temporary:
            output = Path(temporary)
            (output / "benchmark.json").write_text(
                json.dumps({"dry_run": False, "results": []}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "rerun requires explicit authorization"):
                run_benchmark(
                    scene_id="S004",
                    dry_run=False,
                    output_dir=output,
                    provider=provider,
                )

            self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()

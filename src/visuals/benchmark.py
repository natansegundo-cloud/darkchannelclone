"""Benchmark controlado de modelos OpenRouter usando somente a cena S004."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping
from uuid import uuid4

from .engine import ROOT, build_generation_jobs, load_reference_profile
from .prompt_builder import build_prompt
from .providers import (
    GenerationRequest,
    OpenRouterImageProvider,
    UNKNOWN_BILLED_TIMEOUT,
    VisualProvider,
)
from .references import resolve_scene_references


DEFAULT_BENCHMARK_CONFIG = ROOT / "config" / "visual_benchmark.json"
DEFAULT_BENCHMARK_OUTPUT = ROOT / "output" / "model_benchmark" / "S004"
DEFAULT_ENV_FILE = ROOT / "scripts" / ".env"
MAX_BENCHMARK_COST_USD = Decimal("0.15")


def load_benchmark_config(path: Path = DEFAULT_BENCHMARK_CONFIG) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_benchmark_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_models = [
        "sourceful/riverflow-v2.5-fast:free",
        "openai/gpt-image-2",
        "black-forest-labs/flux.2-klein-4b",
        "bytedance-seed/seedream-4.5",
    ]
    if config.get("provider") != "openrouter":
        errors.append("benchmark provider must be openrouter")
    if config.get("scene_id") != "S004":
        errors.append("benchmark scene must be S004")
    if config.get("aspect_ratio") != "16:9":
        errors.append("benchmark aspect_ratio must be 16:9")
    if config.get("resolution") != "1K":
        errors.append("benchmark resolution must be 1K")
    if config.get("n") != 1:
        errors.append("benchmark n must be 1")
    if config.get("max_attempts_per_model") != 1:
        errors.append("benchmark max_attempts_per_model must be 1")
    if Decimal(str(config.get("max_benchmark_cost_usd"))) != MAX_BENCHMARK_COST_USD:
        errors.append("benchmark cost cap must be US$0.15")
    candidates = config.get("candidates", [])
    if [candidate.get("model") for candidate in candidates] != expected_models:
        errors.append("benchmark model list or order is not canonical")
    output_files = [candidate.get("output_file") for candidate in candidates]
    if len(output_files) != len(set(output_files)) or any(not value for value in output_files):
        errors.append("benchmark output filenames must be unique and non-empty")
    return errors


def _local_env_api_key(path: Path) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "OPENROUTER_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or None
    return None


def api_key_configured(env_file: Path = DEFAULT_ENV_FILE) -> bool:
    """Informa presença no ambiente ou env local sem expor ou persistir o segredo."""

    return bool(os.environ.get("OPENROUTER_API_KEY") or _local_env_api_key(env_file))


def configure_api_key_environment(env_file: Path = DEFAULT_ENV_FILE) -> bool:
    """Carrega o env local no processo somente quando a chave ainda não está exportada."""

    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    value = _local_env_api_key(env_file)
    if not value:
        return False
    os.environ["OPENROUTER_API_KEY"] = value
    return True


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


@contextmanager
def _benchmark_lock(output_dir: Path, *, dry_run: bool, run_id: str):
    """Impede dois benchmarks reais concorrentes no mesmo diretório de saída."""

    if dry_run:
        yield
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "benchmark.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"benchmark lock already exists: {lock_path}; verify no benchmark process is running"
        ) from exc
    try:
        os.write(
            descriptor,
            json.dumps(
                {"pid": os.getpid(), "run_id": run_id}, ensure_ascii=False
            ).encode("utf-8"),
        )
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _result_record(
    *,
    model: str,
    status: str,
    elapsed_seconds: float,
    cost_usd: float | None,
    references_used: list[str],
    output_path: str | None,
    error: str | None,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "model": model,
        "status": status,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "cost_usd": cost_usd,
        "references_used": references_used,
        "output_path": output_path,
        "error": error,
        "request_id": request_id,
    }


def _run_benchmark_unlocked(
    *,
    scene_id: str,
    dry_run: bool,
    output_dir: Path = DEFAULT_BENCHMARK_OUTPUT,
    config: Mapping[str, Any] | None = None,
    provider: VisualProvider | None = None,
    run_id: str,
    request_reason: str,
) -> dict[str, Any]:
    benchmark_config = dict(config or load_benchmark_config())
    errors = validate_benchmark_config(benchmark_config)
    if errors:
        raise ValueError("invalid benchmark config: " + "; ".join(errors))
    if scene_id != benchmark_config["scene_id"]:
        raise ValueError("this controlled benchmark accepts only scene S004")

    scene = build_generation_jobs([scene_id])[0]
    profile = load_reference_profile()
    compiled_prompt = build_prompt(scene)
    references = resolve_scene_references(
        scene,
        profile,
        include_scene_reference=bool(benchmark_config.get("include_scene_reference")),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if not dry_run and provider is None:
        configure_api_key_environment()
    image_provider = provider or OpenRouterImageProvider(
        root=ROOT,
        endpoint=str(benchmark_config["endpoint"]),
    )
    total_cost = Decimal("0")
    total_cost_known = True
    stop_reason: str | None = None
    results: list[dict[str, Any]] = []
    called_models: set[str] = set()
    request_sequence = 0
    audit_log_path = output_dir / "request_audit.jsonl"

    for candidate in benchmark_config["candidates"]:
        model = str(candidate["model"])
        output_path = output_dir / str(candidate["output_file"])
        display_output = _display_path(output_path)
        if dry_run:
            results.append(
                _result_record(
                    model=model,
                    status="dry_run",
                    elapsed_seconds=0.0,
                    cost_usd=None,
                    references_used=references,
                    output_path=display_output,
                    error=None,
                    request_id=None,
                )
            )
            continue
        if total_cost >= MAX_BENCHMARK_COST_USD or stop_reason:
            results.append(
                _result_record(
                    model=model,
                    status="skipped_budget",
                    elapsed_seconds=0.0,
                    cost_usd=None,
                    references_used=references,
                    output_path=None,
                    error=stop_reason or "benchmark budget cap reached",
                    request_id=None,
                )
            )
            continue

        if model in called_models:
            raise RuntimeError(f"duplicate benchmark request blocked for model: {model}")
        called_models.add(model)
        request_sequence += 1

        request = GenerationRequest(
            scene_id=scene_id,
            prompt=compiled_prompt,
            visual_profile=str(profile["profile_id"]),
            fin_lock=str(profile["fin_lock"]),
            reference_images=tuple(references),
            provider="openrouter",
            model=model,
            tier="benchmark",
            timeout=int(benchmark_config["timeout"]),
            output_path=output_path,
            aspect_ratio=str(benchmark_config["aspect_ratio"]),
            resolution=str(benchmark_config["resolution"]),
            n=int(benchmark_config["n"]),
            output_format=str(benchmark_config["output_format"]),
            request_sequence=request_sequence,
            request_attempt=1,
            reason=request_reason,
            audit_log_path=audit_log_path,
            run_id=run_id,
        )
        started = perf_counter()
        generation = image_provider.generate(request)
        elapsed = perf_counter() - started
        if generation.cost_usd is not None:
            total_cost += Decimal(str(generation.cost_usd))
        elif generation.status == UNKNOWN_BILLED_TIMEOUT:
            total_cost_known = False
        elif generation.success:
            total_cost_known = False
            stop_reason = "usage.cost missing; benchmark stopped to preserve the cost cap"
        results.append(
            _result_record(
                model=model,
                status=generation.status or ("generated" if generation.success else "failed"),
                elapsed_seconds=elapsed,
                cost_usd=generation.cost_usd,
                references_used=references,
                output_path=display_output if generation.success else None,
                error=generation.error,
                request_id=generation.request_id,
            )
        )
        if generation.status == UNKNOWN_BILLED_TIMEOUT:
            stop_reason = "unknown billed timeout; benchmark stopped to prevent duplicate charges"
            break
        if total_cost >= MAX_BENCHMARK_COST_USD:
            stop_reason = "benchmark budget cap reached"

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "reason": request_reason,
        "scene_id": scene_id,
        "provider": "openrouter",
        "aspect_ratio": benchmark_config["aspect_ratio"],
        "resolution": benchmark_config["resolution"],
        "n": benchmark_config["n"],
        "max_benchmark_cost_usd": float(MAX_BENCHMARK_COST_USD),
        "dry_run": dry_run,
        "request_audit_path": _display_path(audit_log_path) if not dry_run else None,
        "total_cost_usd": float(total_cost) if total_cost_known else None,
        "results": results,
    }
    (output_dir / "benchmark.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def run_benchmark(
    *,
    scene_id: str,
    dry_run: bool,
    output_dir: Path = DEFAULT_BENCHMARK_OUTPUT,
    config: Mapping[str, Any] | None = None,
    provider: VisualProvider | None = None,
    allow_rerun: bool = False,
    rerun_reason: str | None = None,
) -> dict[str, Any]:
    run_id = uuid4().hex
    with _benchmark_lock(output_dir, dry_run=dry_run, run_id=run_id):
        manifest_path = output_dir / "benchmark.json"
        if manifest_path.is_file():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("dry_run") is False:
                if dry_run:
                    raise RuntimeError(
                        "dry-run cannot overwrite a real benchmark; use a different --output"
                    )
                if not allow_rerun:
                    raise RuntimeError(
                        "a real benchmark already exists; rerun requires explicit authorization"
                    )
                if not rerun_reason or not rerun_reason.strip():
                    raise ValueError("an explicit rerun reason is required")
        request_reason = (
            f"benchmark_rerun:{rerun_reason.strip()}"
            if allow_rerun and rerun_reason
            else "benchmark_generation"
        )
        return _run_benchmark_unlocked(
            scene_id=scene_id,
            dry_run=dry_run,
            output_dir=output_dir,
            config=config,
            provider=provider,
            run_id=run_id,
            request_reason=request_reason,
        )

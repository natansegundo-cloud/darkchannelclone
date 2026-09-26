"""Contrato neutro de provider e implementação mock para o pipeline visual."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Protocol


# PNG 1x1 transparente. O mock materializa um artefato verificável sem chamar API.
_MOCK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
UNKNOWN_BILLED_TIMEOUT = "UNKNOWN_BILLED_TIMEOUT"


@dataclass(frozen=True)
class GenerationRequest:
    scene_id: str
    prompt: str
    visual_profile: str
    fin_lock: str
    reference_images: tuple[str, ...]
    provider: str
    model: str
    tier: str
    timeout: int
    output_path: Path
    aspect_ratio: str = "16:9"
    resolution: str = "1K"
    n: int = 1
    output_format: str = "png"
    request_sequence: int | None = None
    request_attempt: int = 1
    reason: str = "generation"
    audit_log_path: Path | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    image_path: Path | None = None
    error: str | None = None
    cost_usd: float | None = None
    request_id: str | None = None
    status: str | None = None


class VisualProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Gera um arquivo de imagem ou retorna uma falha tratável pelo fallback."""


class MockVisualProvider:
    """Provider offline previsível, com falhas injetáveis para testes de fallback."""

    def __init__(
        self,
        *,
        fail_tiers: set[str] | None = None,
        fail_scene_tiers: set[tuple[str, str]] | None = None,
    ) -> None:
        self.fail_tiers = fail_tiers or set()
        self.fail_scene_tiers = fail_scene_tiers or set()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.tier in self.fail_tiers or (
            request.scene_id,
            request.tier,
        ) in self.fail_scene_tiers:
            return GenerationResult(
                success=False,
                error=f"simulated failure for {request.scene_id}/{request.tier}",
            )
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(_MOCK_PNG)
        return GenerationResult(success=True, image_path=request.output_path)


class OpenRouterImageProvider:
    """Cliente mínimo para a Image API do OpenRouter, sem persistir credenciais."""

    ENDPOINT = "https://openrouter.ai/api/v1/images"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        root: Path | None = None,
        endpoint: str = ENDPOINT,
        transport: Callable[..., ContextManager[Any]] | None = None,
    ) -> None:
        self._api_key_override = api_key
        self.root = root or Path(__file__).resolve().parents[2]
        self.endpoint = endpoint
        self._transport = transport or urllib.request.urlopen
        self._request_sequence = 0

    def _api_key(self) -> str | None:
        return self._api_key_override or os.environ.get("OPENROUTER_API_KEY")

    def _input_references(self, references: tuple[str, ...]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for value in references:
            path = Path(value)
            if not path.is_absolute():
                path = self.root / path
            if not path.is_file():
                raise FileNotFoundError(f"reference image not found: {value}")
            suffix = path.suffix.lower()
            media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            encoded.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                }
            )
        return encoded

    @staticmethod
    def _usage_cost(payload: Mapping[str, Any]) -> float | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict) or usage.get("cost") is None:
            return None
        try:
            return float(usage["cost"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_transport_error(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"OpenRouter Image API HTTP {exc.code}"
        if isinstance(exc, urllib.error.URLError):
            return f"OpenRouter Image API unavailable: {exc.reason}"
        return f"OpenRouter Image API error: {type(exc).__name__}"

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        return isinstance(getattr(exc, "reason", None), TimeoutError)

    @staticmethod
    def _request_id(payload: Mapping[str, Any], headers: Any = None) -> str | None:
        for key in ("request_id", "requestId", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        if headers is not None:
            for key in ("x-request-id", "x-openrouter-request-id"):
                value = headers.get(key)
                if value:
                    return str(value)
        return None

    def _append_audit(
        self,
        request: GenerationRequest,
        *,
        sequence: int,
        event: str,
        request_id: str | None = None,
        cost_usd: float | None = None,
        status: str | None = None,
    ) -> None:
        path = request.audit_log_path or (
            self.root / "output" / "openrouter_request_audit.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "run_id": request.run_id,
            "request_sequence": sequence,
            "scene_id": request.scene_id,
            "model": request.model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_attempt": request.request_attempt,
            "reason": request.reason,
            "HTTP_method": "POST",
            "endpoint": self.endpoint,
        }
        if event == "response":
            record.update(
                {
                    "request_id": request_id,
                    "usage_cost": cost_usd,
                    "status": status,
                }
            )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def generate(self, request: GenerationRequest) -> GenerationResult:
        api_key = self._api_key()
        if not api_key:
            return GenerationResult(
                success=False,
                error="OPENROUTER_API_KEY is not configured in the process environment",
            )
        sequence: int | None = None
        http_started = False
        request_id: str | None = None
        try:
            payload = {
                "model": request.model,
                "prompt": request.prompt,
                "n": request.n,
                "aspect_ratio": request.aspect_ratio,
                "resolution": request.resolution,
                "output_format": request.output_format,
                "input_references": self._input_references(request.reference_images),
            }
            body = json.dumps(payload).encode("utf-8")
            http_request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            self._request_sequence += 1
            sequence = request.request_sequence or self._request_sequence
            self._append_audit(
                request,
                sequence=sequence,
                event="request",
            )
            http_started = True
            with self._transport(http_request, timeout=request.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                response_headers = getattr(response, "headers", None)
            cost_usd = self._usage_cost(result)
            request_id = self._request_id(result, response_headers)
            images = result.get("data")
            if not isinstance(images, list) or not images:
                self._append_audit(
                    request,
                    sequence=sequence,
                    event="response",
                    request_id=request_id,
                    cost_usd=cost_usd,
                    status="failed",
                )
                return GenerationResult(
                    success=False,
                    error="OpenRouter response contains no image data",
                    cost_usd=cost_usd,
                    request_id=request_id,
                )
            encoded_image = images[0].get("b64_json")
            if not isinstance(encoded_image, str) or not encoded_image:
                self._append_audit(
                    request,
                    sequence=sequence,
                    event="response",
                    request_id=request_id,
                    cost_usd=cost_usd,
                    status="failed",
                )
                return GenerationResult(
                    success=False,
                    error="OpenRouter response contains no b64_json image",
                    cost_usd=cost_usd,
                    request_id=request_id,
                )
            image_bytes = base64.b64decode(encoded_image, validate=True)
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(image_bytes)
            self._append_audit(
                request,
                sequence=sequence,
                event="response",
                request_id=request_id,
                cost_usd=cost_usd,
                status="generated",
            )
            return GenerationResult(
                success=True,
                image_path=request.output_path,
                cost_usd=cost_usd,
                request_id=request_id,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            billed_timeout = http_started and self._is_timeout_error(exc)
            error = "TimeoutError" if billed_timeout else self._safe_transport_error(exc)
            result_status = UNKNOWN_BILLED_TIMEOUT if billed_timeout else "failed"
            if http_started and sequence is not None:
                headers = getattr(exc, "headers", None)
                if headers is not None:
                    request_id = self._request_id({}, headers)
                self._append_audit(
                    request,
                    sequence=sequence,
                    event="response",
                    request_id=request_id,
                    status=result_status,
                )
            return GenerationResult(
                success=False,
                error=error,
                request_id=request_id,
                status=result_status,
            )


class ProviderRegistry:
    def __init__(self, providers: dict[str, VisualProvider] | None = None) -> None:
        self._providers: dict[str, VisualProvider] = providers or {
            "mock": MockVisualProvider(),
            "openrouter": OpenRouterImageProvider(),
        }

    def get(self, name: str) -> VisualProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ValueError(f"visual provider is not registered: {name}") from exc

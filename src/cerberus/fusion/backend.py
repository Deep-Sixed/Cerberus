"""Provider-neutral Fusion backend seam plus the OpenRouter Fusion implementation.

Cerberus never executes a panel itself. It resolves policy (which alias, which
panel, which analyst, which credential) into a ``FusionRequest`` and hands it to
a ``FusionBackend``. The backend returns either a normalized ``FusionResult`` or
raises ``FusionError`` with the HTTP status and telemetry outcome Cerberus should
report. Nothing outside this module knows the wire shape of a given backend.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from cerberus.telemetry import AttemptOutcome

FusionOutcome = Literal["upstream_error", "fusion_unavailable"]

# OpenRouter Fusion Router server tool.
OPENROUTER_FUSION_TOOL = "openrouter:fusion"


@dataclass(frozen=True, slots=True)
class FusionRequest:
    """Everything a backend needs, already resolved by Cerberus policy."""

    body: dict[str, Any]  # caller generation fields Cerberus allowed through
    panel_models: list[str]  # provider-native model ids, in policy order
    analyst_model: str
    outer_model: str  # writes the final answer from the analysis; policy-validated
    base_url: str
    api_key: str
    timeout_seconds: float  # absolute deadline for the whole call


@dataclass(frozen=True, slots=True)
class FusionResult:
    payload: dict[str, Any]  # OpenAI-compatible completion as returned upstream
    returned_model: str | None  # exactly what the response said, or None
    generation_id: str | None
    usage: dict[str, Any] | None
    http_status: int  # upstream status
    latency_ms: float
    # metadata OBSERVED in the response (e.g. the serving provider); never
    # populated from what was requested
    metadata: dict[str, Any] = field(default_factory=dict)


class FusionError(Exception):
    """A backend failure with the status and outcome Cerberus reports; fail closed.

    ``http_status`` is what Cerberus answers the caller; ``upstream_status`` is
    what the backend actually returned (None when no HTTP response arrived), so
    telemetry keeps the upstream truth rather than the translated status.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        outcome: FusionOutcome,
        attempt_outcome: AttemptOutcome,
        latency_ms: float,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.upstream_status = upstream_status
        self.outcome = outcome
        self.attempt_outcome = attempt_outcome
        self.latency_ms = latency_ms


class FusionBackend(Protocol):
    name: str

    async def execute(self, request: FusionRequest) -> FusionResult: ...

    async def aclose(self) -> None: ...


class OpenRouterFusionBackend:
    """Deliberate through OpenRouter's managed Fusion Router.

    One chat-completions call whose ``model`` is the policy-validated outer
    model, with the ``openrouter:fusion`` server tool naming the panel
    (``analysis_models``) and analyst (``model``). ``tool_choice: required``
    forces the deliberation on every request, so the outer model can never skip
    the panel Cerberus policy selected. The ``openrouter/fusion`` alias is
    deliberately not used: it lets OpenRouter pick the outer model outside
    Cerberus's registry and cost policy. OpenRouter does not expose per-seat
    outcomes, so none are reported here.
    """

    name = "openrouter"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(transport=transport)

    @staticmethod
    def build_body(request: FusionRequest) -> dict[str, Any]:
        body = {k: v for k, v in request.body.items() if k not in ("model", "tools", "tool_choice", "stream")}
        body["model"] = request.outer_model
        body["tools"] = [
            {
                "type": OPENROUTER_FUSION_TOOL,
                "parameters": {
                    "analysis_models": list(request.panel_models),
                    "model": request.analyst_model,
                },
            }
        ]
        body["tool_choice"] = "required"
        return body

    async def execute(self, request: FusionRequest) -> FusionResult:
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            # httpx timeouts are per operation (connect/read/write), so a slowly
            # trickling response could outlive the alias deadline; the asyncio
            # scope makes timeout_seconds an absolute wall-clock budget. Local
            # cancellation does not stop upstream work already billed.
            async with asyncio.timeout(request.timeout_seconds):
                response = await self._client.post(
                    f"{request.base_url.rstrip('/')}/chat/completions",
                    json=self.build_body(request),
                    headers={"authorization": f"Bearer {request.api_key}", "content-type": "application/json"},
                    timeout=request.timeout_seconds,
                )
        except TimeoutError:
            raise FusionError(
                "Fusion deadline exceeded",
                http_status=504,
                outcome="fusion_unavailable",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise FusionError(
                "Fusion backend unreachable",
                http_status=503,
                outcome="fusion_unavailable",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None
        except httpx.HTTPError:
            raise FusionError(
                "Fusion backend request failed",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None

        if response.status_code >= 400:
            raise FusionError(
                f"Fusion backend returned HTTP {response.status_code}",
                http_status=502 if response.status_code < 500 else 503,
                outcome="upstream_error" if response.status_code < 500 else "fusion_unavailable",
                attempt_outcome="retryable_status" if response.status_code in (408, 429) or response.status_code >= 500 else "invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError:
            raise FusionError(
                "Fusion backend returned invalid JSON",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            ) from None

        if not _has_completion_content(payload):
            raise FusionError(
                "Fusion backend returned no completion content",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            )

        usage = payload.get("usage")
        # Only what the response actually says. OpenRouter confirms router use via
        # its generation metadata endpoint, which this call does not consult, so
        # no router attribution is asserted here.
        metadata = {"provider": payload["provider"]} if isinstance(payload.get("provider"), str) else {}
        return FusionResult(
            payload=payload,
            returned_model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            generation_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            usage=usage if isinstance(usage, dict) else None,
            http_status=response.status_code,
            latency_ms=elapsed(),
            metadata=metadata,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _has_completion_content(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    if not isinstance(first, dict):
        return False
    message = first.get("message")
    return isinstance(message, dict) and isinstance(message.get("content"), str) and bool(message["content"])

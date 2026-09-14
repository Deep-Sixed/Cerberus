"""Provider-neutral Fusion backend seam plus the OpenRouter Fusion implementation.

Cerberus never executes a panel itself. It resolves policy (which alias, which
panel, which analyst, which credential) into a ``FusionRequest`` and hands it to
a ``FusionBackend``. The backend returns either a normalized ``FusionResult`` or
raises ``FusionError`` with the HTTP status and telemetry outcome Cerberus should
report. Nothing outside this module knows the wire shape of a given backend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from cerberus.telemetry import AttemptOutcome

FusionOutcome = Literal["upstream_error", "fusion_unavailable"]

# OpenRouter Fusion Router constants — see https://openrouter.ai/docs/guides/features/plugins/fusion
OPENROUTER_FUSION_MODEL = "openrouter/fusion"
OPENROUTER_FUSION_PLUGIN = "fusion"


@dataclass(frozen=True, slots=True)
class FusionRequest:
    """Everything a backend needs, already resolved by Cerberus policy."""

    body: dict[str, Any]  # the caller's OpenAI-compatible body, stream stripped
    panel_models: list[str]  # provider-native model ids, in policy order
    analyst_model: str
    base_url: str
    api_key: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class FusionResult:
    payload: dict[str, Any]  # OpenAI-compatible completion as returned upstream
    returned_model: str | None
    generation_id: str | None
    usage: dict[str, Any] | None
    http_status: int
    latency_ms: float
    # provider/router metadata the backend can vouch for; never inferred
    metadata: dict[str, Any] = field(default_factory=dict)


class FusionError(Exception):
    """A backend failure with the status and outcome Cerberus reports; fail closed."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        outcome: FusionOutcome,
        attempt_outcome: AttemptOutcome,
        latency_ms: float,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.outcome = outcome
        self.attempt_outcome = attempt_outcome
        self.latency_ms = latency_ms


class FusionBackend(Protocol):
    name: str

    async def execute(self, request: FusionRequest) -> FusionResult: ...

    async def aclose(self) -> None: ...


class OpenRouterFusionBackend:
    """Deliberate through OpenRouter's managed Fusion Router.

    One chat-completions call with ``model: openrouter/fusion`` and a ``fusion``
    plugin naming the panel (``analysis_models``) and analyst (``model``).
    ``tool_choice: required`` forces the deliberation on every request, so the
    outer model can never skip the panel Cerberus policy selected. OpenRouter
    does not expose per-seat outcomes, so none are reported here.
    """

    name = "openrouter"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(transport=transport)

    @staticmethod
    def build_body(request: FusionRequest) -> dict[str, Any]:
        body = {k: v for k, v in request.body.items() if k not in ("model", "plugins", "tool_choice", "stream")}
        body["model"] = OPENROUTER_FUSION_MODEL
        body["plugins"] = [
            {
                "id": OPENROUTER_FUSION_PLUGIN,
                "analysis_models": list(request.panel_models),
                "model": request.analyst_model,
            }
        ]
        body["tool_choice"] = "required"
        return body

    async def execute(self, request: FusionRequest) -> FusionResult:
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            response = await self._client.post(
                f"{request.base_url.rstrip('/')}/chat/completions",
                json=self.build_body(request),
                headers={"authorization": f"Bearer {request.api_key}", "content-type": "application/json"},
                timeout=request.timeout_seconds,
            )
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
            ) from None

        if not _has_completion_content(payload):
            raise FusionError(
                "Fusion backend returned no completion content",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="invalid_response",
                latency_ms=elapsed(),
            )

        usage = payload.get("usage")
        return FusionResult(
            payload=payload,
            returned_model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            generation_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            usage=usage if isinstance(usage, dict) else None,
            http_status=response.status_code,
            latency_ms=elapsed(),
            metadata={"router": OPENROUTER_FUSION_MODEL, "provider": payload.get("provider")}
            if isinstance(payload.get("provider"), str)
            else {"router": OPENROUTER_FUSION_MODEL},
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

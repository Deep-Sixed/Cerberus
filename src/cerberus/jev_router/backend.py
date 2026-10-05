"""Provider-neutral Jev Router backend seam plus the OpenRouter implementation.

Cerberus resolves which models may serve a jev-router alias, filters them, and
hands the survivors to a ``JevRouterBackend`` as a ``JevRouterRequest``. The
backend makes one routed call and returns a normalized ``JevRouterResult``,
including the router's own account of its decision, or raises
``JevRouterError``. Whether that decision stayed inside the pool is Cerberus
policy and is judged by the caller of this module, not here; nothing outside
this module knows the wire shape of a given backend.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from cerberus.telemetry import AttemptOutcome

JevRouterOutcome = Literal["upstream_error", "jev_router_unavailable"]

# OpenRouter's hosted Jev Router and the plugin that narrows its pool.
JEV_ROUTER_MODEL = "typesafe/jev-router"
JEV_ROUTER_PLUGIN = "jev-router"
# Asks OpenRouter to attach openrouter_metadata, whose jev-router pipeline stage
# is the only evidence of whether the include list was honoured.
METADATA_HEADER = "x-openrouter-metadata"

# "openai/gpt-6-luna" also matches its dated revisions, "openai/gpt-6-luna-20260922".
_DATED_REVISION = re.compile(r"-\d{8}")


@dataclass(frozen=True, slots=True)
class JevRouterRequest:
    """Everything a backend needs, already resolved and filtered by Cerberus."""

    body: dict[str, Any]  # caller generation fields Cerberus allowed through
    pool_models: list[str]  # exact provider-native slugs; never empty, never a pattern
    base_url: str
    api_key: str
    timeout_seconds: float  # absolute deadline for the whole call


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """The router's own report of what it did, exactly as observed."""

    resolved_models: list[str] | None  # models the router selected, in fallback order
    list_fallback: str | None  # e.g. "models_ignored": the include list was NOT applied
    list_tier_cap: str | None
    max_fallback: str | None

    def record(self) -> dict[str, Any]:
        return {
            "resolved_models": self.resolved_models,
            "list_fallback": self.list_fallback,
            "list_tier_cap": self.list_tier_cap,
            "max_fallback": self.max_fallback,
        }


@dataclass(frozen=True, slots=True)
class JevRouterResult:
    payload: dict[str, Any]  # OpenAI-compatible completion with router metadata removed
    returned_model: str | None  # exactly what the response said, or None
    generation_id: str | None
    decision: RouterDecision | None  # None when the response carried no jev-router stage
    http_status: int
    latency_ms: float
    # metadata OBSERVED in the response (e.g. the serving provider); never
    # populated from what was requested
    metadata: dict[str, Any] = field(default_factory=dict)


class JevRouterError(Exception):
    """A backend failure with the status and outcome Cerberus reports; fail closed.

    ``http_status`` is what Cerberus answers the caller; ``upstream_status`` is
    what the backend actually returned (None when no HTTP response arrived).
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        outcome: JevRouterOutcome,
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


class JevRouterBackend(Protocol):
    name: str

    async def execute(self, request: JevRouterRequest) -> JevRouterResult: ...

    async def aclose(self) -> None: ...


def slug_matches(served: str, registered: str) -> bool:
    """Whether a served model is the registered slug or one of its dated revisions.

    The jev-router plugin documents that an exact slug matches every dated
    revision of it, so the model a response names may carry a date suffix the
    registry entry does not. Nothing looser is accepted.
    """

    if served == registered:
        return True
    suffix = served.removeprefix(registered)
    return suffix != served and _DATED_REVISION.fullmatch(suffix) is not None


class OpenRouterJevRouterBackend:
    """Route through OpenRouter's hosted Jev Router, narrowed to the Cerberus pool.

    One chat-completions call whose ``model`` is ``typesafe/jev-router`` and
    whose ``jev-router`` plugin lists the pool as exact slugs. Router metadata
    is always requested, because it is the only place OpenRouter says whether
    the list was applied; it is parsed here and removed from the payload, so a
    caller gets the completion shape it would have had without it.
    """

    name = "openrouter"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(transport=transport)

    @staticmethod
    def build_body(request: JevRouterRequest) -> dict[str, Any]:
        # stream_options is only valid with stream: true, and this call never streams
        body = {
            k: v for k, v in request.body.items() if k not in ("model", "plugins", "stream", "stream_options")
        }
        body["model"] = JEV_ROUTER_MODEL
        body["plugins"] = [{"id": JEV_ROUTER_PLUGIN, "models": list(request.pool_models)}]
        return body

    async def execute(self, request: JevRouterRequest) -> JevRouterResult:
        if not request.pool_models:
            # An empty include list matches nothing, and OpenRouter answers a
            # list that matches nothing by routing over its whole pool.
            raise ValueError("jev-router request requires a non-empty pool")
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            # httpx timeouts are per operation, so the asyncio scope makes
            # timeout_seconds an absolute wall-clock budget. Local cancellation
            # does not stop upstream work already billed.
            async with asyncio.timeout(request.timeout_seconds):
                response = await self._client.post(
                    f"{request.base_url.rstrip('/')}/chat/completions",
                    json=self.build_body(request),
                    headers={
                        "authorization": f"Bearer {request.api_key}",
                        "content-type": "application/json",
                        METADATA_HEADER: "enabled",
                    },
                    timeout=request.timeout_seconds,
                )
        except TimeoutError:
            raise JevRouterError(
                "Jev Router deadline exceeded",
                http_status=504,
                outcome="jev_router_unavailable",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise JevRouterError(
                "Jev Router unreachable",
                http_status=503,
                outcome="jev_router_unavailable",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None
        except httpx.HTTPError:
            raise JevRouterError(
                "Jev Router request failed",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="transport_error",
                latency_ms=elapsed(),
            ) from None

        if response.status_code >= 400:
            # A 404 here is the plugin saying the lists left no admitted model.
            raise JevRouterError(
                f"Jev Router returned HTTP {response.status_code}",
                http_status=502 if response.status_code < 500 else 503,
                outcome="upstream_error" if response.status_code < 500 else "jev_router_unavailable",
                attempt_outcome="retryable_status" if response.status_code in (408, 429) or response.status_code >= 500 else "invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError:
            raise JevRouterError(
                "Jev Router returned invalid JSON",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            ) from None

        if not _has_completion_content(payload):
            raise JevRouterError(
                "Jev Router returned no completion content",
                http_status=502,
                outcome="upstream_error",
                attempt_outcome="invalid_response",
                latency_ms=elapsed(),
                upstream_status=response.status_code,
            )

        metadata = {"provider": payload["provider"]} if isinstance(payload.get("provider"), str) else {}
        return JevRouterResult(
            payload={k: v for k, v in payload.items() if k != "openrouter_metadata"},
            returned_model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            generation_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            decision=_router_decision(payload),
            http_status=response.status_code,
            latency_ms=elapsed(),
            metadata=metadata,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _router_decision(payload: dict[str, Any]) -> RouterDecision | None:
    """The jev-router stage of ``openrouter_metadata.pipeline``, or None if absent.

    Only the documented fields are kept, and only in the shapes documented for
    them, so nothing else the upstream attaches reaches telemetry.
    """

    metadata = payload.get("openrouter_metadata")
    pipeline = metadata.get("pipeline") if isinstance(metadata, dict) else None
    if not isinstance(pipeline, list):
        return None
    for stage in pipeline:
        if isinstance(stage, dict) and stage.get("name") == JEV_ROUTER_PLUGIN:
            data = stage.get("data")
            data = data if isinstance(data, dict) else {}
            resolved = data.get("resolved_models")
            return RouterDecision(
                resolved_models=(
                    [m for m in resolved if isinstance(m, str)] if isinstance(resolved, list) else None
                ),
                list_fallback=_scalar(data.get("list_fallback")),
                list_tier_cap=_scalar(data.get("list_tier_cap")),
                max_fallback=_scalar(data.get("max_fallback")),
            )
    return None


def _scalar(value: Any) -> str | None:
    """A documented scalar field as a string; None when absent, null, false or empty."""

    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return "true"
    if isinstance(value, (str, int, float)):
        return str(value)
    # present but not a scalar: keep that it was present, never its content
    return "unrecognized"


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
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    # a routed request may legitimately answer with a function-tool call
    return (isinstance(content, str) and bool(content)) or bool(message.get("tool_calls"))

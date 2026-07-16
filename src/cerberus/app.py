"""OpenAI Chat Completions API backed by portable Cerberus policy."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import secrets
import time
from typing import Any
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
import httpx

from .config import RouterConfig, load_config
from .routing import Router, RoutingError, Selection
from .telemetry import RoutingAttempt, RoutingEvent, RoutingOutcome, TelemetryEmitter

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _upstream_body(body: dict[str, Any], selection: Selection) -> dict[str, Any]:
    upstream = dict(body)
    upstream.pop("request_type", None)
    upstream.pop("lane", None)
    upstream["model"] = selection.model
    return upstream


def _metadata(selection: Selection, request_id: str, attempts: int) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "provider": selection.provider_id,
        "pool": selection.pool,
        "model": selection.model,
        "used_fallback": selection.used_fallback,
        "attempts": attempts,
    }


def _token_usage(body: dict[str, Any]) -> dict[str, int] | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    safe_usage = {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance((value := usage.get(key)), int) and not isinstance(value, bool) and value >= 0
    }
    return safe_usage or None


class _StreamingUsageCollector:
    """Extract usage-only SSE fields without retaining response content."""

    _MAX_LINE_BYTES = 65_536

    def __init__(self) -> None:
        self._buffer = b""
        self.usage: dict[str, int] | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._read_line(line)
        if len(self._buffer) > self._MAX_LINE_BYTES:
            self._buffer = b""

    def finish(self) -> None:
        if self._buffer:
            self._read_line(self._buffer)
            self._buffer = b""

    def _read_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line.removeprefix(b"data:").strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            body = json.loads(payload)
        except json.JSONDecodeError, UnicodeDecodeError:
            return
        if isinstance(body, dict) and (usage := _token_usage(body)) is not None:
            self.usage = usage


def _routing_event(
    *,
    request_id: str,
    request_type: str,
    selection: Selection | None,
    attempts: list[RoutingAttempt],
    http_status: int,
    outcome: RoutingOutcome,
    started_at: float,
    token_usage: dict[str, int] | None,
    streaming: bool,
) -> RoutingEvent:
    return RoutingEvent(
        request_id=request_id,
        request_type=request_type,
        provider=selection.provider_id if selection else None,
        pool=selection.pool if selection else None,
        model=selection.model if selection else None,
        used_fallback=selection.used_fallback if selection else False,
        attempts=attempts,
        http_status=http_status,
        outcome=outcome,
        latency_ms=(time.perf_counter() - started_at) * 1000,
        token_usage=token_usage,
        timestamp=datetime.now(timezone.utc),
        streaming=streaming,
    )


async def dispatch(
    *,
    body: dict[str, Any],
    router: Router,
    client: httpx.AsyncClient,
    telemetry: TelemetryEmitter,
) -> JSONResponse | StreamingResponse:
    request_id = str(uuid.uuid4())
    request_type = router.canonical_request_type(body.get("request_type"))
    streaming = bool(body.get("stream", False))
    attempted: set[str] = set()
    attempts: list[RoutingAttempt] = []
    started_at = time.perf_counter()
    last_selection: Selection | None = None

    while True:
        try:
            selection = router.select(request_type, excluded=attempted)
        except RoutingError as exc:
            telemetry.emit(
                _routing_event(
                    request_id=request_id,
                    request_type=request_type,
                    selection=last_selection,
                    attempts=attempts,
                    http_status=503,
                    outcome="routing_exhausted",
                    started_at=started_at,
                    token_usage=None,
                    streaming=streaming,
                )
            )
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "No provider available", "detail": str(exc), "request_id": request_id}},
            )

        provider = router.config.providers[selection.provider_id]
        last_selection = selection
        attempt_started = time.perf_counter()
        headers: dict[str, str] = {"content-type": "application/json"}
        if provider.api_key_env:
            api_key = os.environ.get(provider.api_key_env, "").strip()
            if not api_key:
                attempts.append(
                    RoutingAttempt(
                        provider=selection.provider_id,
                        pool=selection.pool,
                        model=selection.model,
                        used_fallback=selection.used_fallback,
                        outcome="missing_credentials",
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                    )
                )
                router.cool(selection.provider_id)
                attempted.add(selection.provider_id)
                continue
            headers["authorization"] = f"Bearer {api_key}"

        url = str(provider.base_url).rstrip("/") + "/chat/completions"
        request = client.build_request("POST", url, headers=headers, json=_upstream_body(body, selection))
        try:
            response = await client.send(request, stream=streaming)
        except httpx.HTTPError:
            attempts.append(
                RoutingAttempt(
                    provider=selection.provider_id,
                    pool=selection.pool,
                    model=selection.model,
                    used_fallback=selection.used_fallback,
                    outcome="transport_error",
                    latency_ms=(time.perf_counter() - attempt_started) * 1000,
                )
            )
            router.cool(selection.provider_id)
            attempted.add(selection.provider_id)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            attempts.append(
                RoutingAttempt(
                    provider=selection.provider_id,
                    pool=selection.pool,
                    model=selection.model,
                    used_fallback=selection.used_fallback,
                    outcome="retryable_status",
                    latency_ms=(time.perf_counter() - attempt_started) * 1000,
                    http_status=response.status_code,
                )
            )
            if response.status_code == 429:
                router.cool(selection.provider_id, _retry_after_seconds(response))
            await response.aclose()
            attempted.add(selection.provider_id)
            continue

        metadata = _metadata(selection, request_id, len(attempted) + 1)
        final_attempt = RoutingAttempt(
            provider=selection.provider_id,
            pool=selection.pool,
            model=selection.model,
            used_fallback=selection.used_fallback,
            outcome="response",
            latency_ms=(time.perf_counter() - attempt_started) * 1000,
            http_status=response.status_code,
        )
        attempts.append(final_attempt)
        if streaming:
            usage_collector = _StreamingUsageCollector()

            async def proxy() -> Any:
                completed = False
                try:
                    async for chunk in response.aiter_bytes():
                        usage_collector.feed(chunk)
                        yield chunk
                    completed = True
                finally:
                    usage_collector.finish()
                    final_attempt.latency_ms = (time.perf_counter() - attempt_started) * 1000
                    await response.aclose()
                    outcome: RoutingOutcome
                    if not completed:
                        final_attempt.outcome = "stream_interrupted"
                        outcome = "stream_interrupted"
                    elif 200 <= response.status_code < 400:
                        outcome = "success"
                    else:
                        outcome = "upstream_error"
                    telemetry.emit(
                        _routing_event(
                            request_id=request_id,
                            request_type=request_type,
                            selection=selection,
                            attempts=attempts,
                            http_status=response.status_code,
                            outcome=outcome,
                            started_at=started_at,
                            token_usage=usage_collector.usage,
                            streaming=True,
                        )
                    )

            return StreamingResponse(
                proxy(),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "text/event-stream"),
                headers={
                    "x-request-id": request_id,
                    "x-cerberus-provider": selection.provider_id,
                    "x-cerberus-pool": selection.pool,
                },
            )

        try:
            response_body = response.json() if response.content else {}
        except json.JSONDecodeError, UnicodeDecodeError:
            final_attempt.outcome = "invalid_response"
            final_attempt.latency_ms = (time.perf_counter() - attempt_started) * 1000
            telemetry.emit(
                _routing_event(
                    request_id=request_id,
                    request_type=request_type,
                    selection=selection,
                    attempts=attempts,
                    http_status=502,
                    outcome="upstream_error",
                    started_at=started_at,
                    token_usage=None,
                    streaming=False,
                )
            )
            await response.aclose()
            return JSONResponse(
                status_code=502,
                content={"error": {"message": "Upstream response was not valid JSON", "request_id": request_id}},
            )
        await response.aclose()
        telemetry.emit(
            _routing_event(
                request_id=request_id,
                request_type=request_type,
                selection=selection,
                attempts=attempts,
                http_status=response.status_code,
                outcome="success" if 200 <= response.status_code < 400 else "upstream_error",
                started_at=started_at,
                token_usage=_token_usage(response_body) if isinstance(response_body, dict) else None,
                streaming=False,
            )
        )
        if isinstance(response_body, dict):
            response_body = {**response_body, "cerberus": metadata}
        return JSONResponse(
            content=response_body, status_code=response.status_code, headers={"x-request-id": request_id}
        )


def create_app(
    config: RouterConfig | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    telemetry_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = config or load_config()
    router = Router(config)
    telemetry = TelemetryEmitter(config.telemetry, telemetry_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(transport=http_transport, timeout=300.0)
        await telemetry.start()
        try:
            yield
        finally:
            await telemetry.close()
            await app.state.http_client.aclose()

    app = FastAPI(title="Cerberus v3", version="0.1.0", lifespan=lifespan)

    def authenticated(request: Request) -> bool:
        if config.server.api_token_env is None:
            return True
        expected = os.environ.get(config.server.api_token_env, "")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        return bool(expected) and secrets.compare_digest(supplied, expected)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "cerberus-v3", "cooldowns": router.cooldown_state()}

    @app.get("/v1/models", response_model=None)
    async def models(request: Request) -> dict[str, Any] | JSONResponse:
        if not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        return {
            "object": "list",
            "data": [
                {"id": provider.model, "object": "model", "owned_by": provider_id}
                for provider_id, provider in config.providers.items()
            ],
        }

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        if not authenticated(request):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized"}})
        try:
            body = await request.json()
        except json.JSONDecodeError, UnicodeDecodeError:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "JSON object body required"}})
        return await dispatch(body=body, router=router, client=request.app.state.http_client, telemetry=telemetry)

    return app

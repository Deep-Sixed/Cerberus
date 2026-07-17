"""The failover loop — ordered candidates, scoped cooldowns, exclusion telemetry.

One loop serves every alias mode (SPEC.md: the heads are policies, not routers).
Fusion-mode aliases divert to the worker in Session 11.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from cerberus.egress.client import (
    RETRYABLE_STATUS_CODES,
    StreamingUsageCollector,
    build_upstream_request,
    retry_after_seconds,
    token_usage,
)
from cerberus.registry.loader import ConfigDocument
from cerberus.router.engine import Target, cost_eligible, ordered_targets
from cerberus.state.cooldowns import InMemoryCooldownStore
from cerberus.telemetry import RoutingAttempt, RoutingEvent, RoutingOutcome, TelemetryEmitter


def _attempt(target: Target, outcome: str, started: float, http_status: int | None = None) -> RoutingAttempt:
    return RoutingAttempt(
        provider=f"{target.provider_id}/{target.credential_id}",
        pool=target.model,
        model=target.model,
        used_fallback=False,
        outcome=outcome,  # type: ignore[arg-type]
        latency_ms=(time.perf_counter() - started) * 1000,
        http_status=http_status,
    )


def _event(
    *,
    request_id: str,
    alias: str,
    mode: str,
    target: Target | None,
    attempts: list[RoutingAttempt],
    http_status: int,
    outcome: RoutingOutcome,
    started_at: float,
    usage: dict[str, int] | None,
    streaming: bool,
    fallback: bool,
) -> RoutingEvent:
    # S2 bridge onto the donor event shape: request_type carries the alias,
    # pool carries the mode. S8 replaces this with the unified Cerberus schema.
    return RoutingEvent(
        request_id=request_id,
        request_type=alias,
        provider=target.provider_id if target else None,
        pool=mode,
        model=target.model if target else None,
        used_fallback=fallback,
        attempts=attempts,
        http_status=http_status,
        outcome=outcome,
        latency_ms=(time.perf_counter() - started_at) * 1000,
        token_usage=usage,
        timestamp=datetime.now(timezone.utc),
        streaming=streaming,
    )


async def dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    document: ConfigDocument,
    store: InMemoryCooldownStore,
    client: httpx.AsyncClient,
    telemetry: TelemetryEmitter,
) -> JSONResponse | StreamingResponse:
    request_id = str(uuid.uuid4())
    config = document.config
    alias = config.aliases[alias_name]
    started_at = time.perf_counter()
    streaming = bool(body.get("stream", False))

    targets = ordered_targets(config, alias_name)
    eligible, exclusions = cost_eligible(alias, targets)
    attempts: list[RoutingAttempt] = []
    attempted = 0

    for index, target in enumerate(eligible):
        cooldown = store.active_for(target.provider_id, target.credential_id, target.model)
        if cooldown is not None:
            exclusions.append(
                {**target.describe(), "reason": f"cooldown_{cooldown.reason}", "scope": cooldown.scope}
            )
            continue

        api_key = os.environ.get(target.api_key_env, "").strip()
        attempt_started = time.perf_counter()
        if not api_key:
            exclusions.append({**target.describe(), "reason": "missing_credentials"})
            attempts.append(_attempt(target, "missing_credentials", attempt_started))
            continue

        attempted += 1
        request = build_upstream_request(client, target, body, api_key)
        try:
            response = await client.send(request, stream=streaming)
        except httpx.HTTPError:
            attempts.append(_attempt(target, "transport_error", attempt_started))
            store.apply(
                scope="model",
                provider=target.provider_id,
                credential=target.credential_id,
                model=target.model,
                reason="transport_error",
                duration_seconds=target.transport_cooldown_seconds,
            )
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            attempts.append(_attempt(target, "retryable_status", attempt_started, response.status_code))
            if response.status_code == 429:
                retry_after = retry_after_seconds(response)
                duration = (
                    min(retry_after, target.quota_cooldown_seconds)
                    if retry_after is not None
                    else target.quota_cooldown_seconds
                )
                # model scope by default; credential/provider escalation policy lands in S3
                store.apply(
                    scope="model",
                    provider=target.provider_id,
                    credential=target.credential_id,
                    model=target.model,
                    reason="quota_429",
                    duration_seconds=duration,
                )
            await response.aclose()
            continue

        final_attempt = _attempt(target, "response", attempt_started, response.status_code)
        attempts.append(final_attempt)
        used_fallback = index > 0
        metadata = {
            "request_id": request_id,
            "alias": alias_name,
            "mode": alias.mode,
            **target.describe(),
            "attempts": attempted,
            "config_version": document.version,
        }

        if streaming:
            collector = StreamingUsageCollector()

            async def proxy() -> Any:
                completed = False
                try:
                    async for chunk in response.aiter_bytes():
                        collector.feed(chunk)
                        yield chunk
                    completed = True
                finally:
                    collector.finish()
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
                        _event(
                            request_id=request_id,
                            alias=alias_name,
                            mode=alias.mode,
                            target=target,
                            attempts=attempts,
                            http_status=response.status_code,
                            outcome=outcome,
                            started_at=started_at,
                            usage=collector.usage,
                            streaming=True,
                            fallback=used_fallback,
                        )
                    )

            return StreamingResponse(
                proxy(),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "text/event-stream"),
                headers={
                    "x-request-id": request_id,
                    "x-cerberus-provider": target.provider_id,
                    "x-cerberus-model": target.model,
                },
            )

        try:
            response_body = response.json() if response.content else {}
        except json.JSONDecodeError, UnicodeDecodeError:
            final_attempt.outcome = "invalid_response"
            final_attempt.latency_ms = (time.perf_counter() - attempt_started) * 1000
            await response.aclose()
            telemetry.emit(
                _event(
                    request_id=request_id,
                    alias=alias_name,
                    mode=alias.mode,
                    target=target,
                    attempts=attempts,
                    http_status=502,
                    outcome="upstream_error",
                    started_at=started_at,
                    usage=None,
                    streaming=False,
                    fallback=used_fallback,
                )
            )
            return JSONResponse(
                status_code=502,
                content={"error": {"message": "Upstream response was not valid JSON", "request_id": request_id}},
            )
        await response.aclose()
        telemetry.emit(
            _event(
                request_id=request_id,
                alias=alias_name,
                mode=alias.mode,
                target=target,
                attempts=attempts,
                http_status=response.status_code,
                outcome="success" if 200 <= response.status_code < 400 else "upstream_error",
                started_at=started_at,
                usage=token_usage(response_body) if isinstance(response_body, dict) else None,
                streaming=False,
                fallback=used_fallback,
            )
        )
        if isinstance(response_body, dict):
            response_body = {**response_body, "cerberus": metadata}
        return JSONResponse(
            content=response_body, status_code=response.status_code, headers={"x-request-id": request_id}
        )

    telemetry.emit(
        _event(
            request_id=request_id,
            alias=alias_name,
            mode=alias.mode,
            target=None,
            attempts=attempts,
            http_status=503,
            outcome="routing_exhausted",
            started_at=started_at,
            usage=None,
            streaming=streaming,
            fallback=False,
        )
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": "No provider available",
                "request_id": request_id,
                "exclusions": exclusions,
            }
        },
    )


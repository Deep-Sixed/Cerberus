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


def _attempt(
    target: Target,
    outcome: str,
    started: float,
    http_status: int | None = None,
    *,
    fallback: bool,
    cooldown_scope: str | None = None,
) -> RoutingAttempt:
    return RoutingAttempt(
        provider=f"{target.provider_id}/{target.credential_id}",
        pool=target.model,
        model=target.model,
        used_fallback=fallback,
        outcome=outcome,  # type: ignore[arg-type]
        latency_ms=(time.perf_counter() - started) * 1000,
        http_status=http_status,
        cooldown_scope=cooldown_scope,
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
    identity: str | None,
    config_version: str | None = None,
    candidates: list[str] | None = None,
    exclusions: list[dict] | None = None,
) -> RoutingEvent:
    return RoutingEvent(
        request_id=request_id,
        alias=alias,
        provider=target.provider_id if target else None,
        mode=mode,
        model=target.model if target else None,
        used_fallback=fallback,
        credential=target.credential_id if target else None,
        cost_tier=target.cost_tier if target else None,
        candidates=candidates,
        exclusions=exclusions,
        attempts=attempts,
        http_status=http_status,
        outcome=outcome,
        latency_ms=(time.perf_counter() - started_at) * 1000,
        token_usage=usage,
        timestamp=datetime.now(timezone.utc),
        streaming=streaming,
        identity=identity,
        config_version=config_version,
    )


def unauthorized_event(
    *, alias_name: str, mode: str, identity: str | None, config_version: str | None = None
) -> RoutingEvent:
    return _event(
        request_id=str(uuid.uuid4()),
        alias=alias_name,
        mode=mode,
        target=None,
        attempts=[],
        http_status=403,
        outcome="unauthorized",
        started_at=time.perf_counter(),
        usage=None,
        streaming=False,
        fallback=False,
        identity=identity,
        config_version=config_version,
    )


def shadow_decision_event(
    *,
    document: ConfigDocument,
    alias_name: str,
    store,
    identity: str | None,
) -> RoutingEvent | None:
    """Policy-only evaluation of a candidate config. MUST NOT touch the network:
    no http client is even reachable from here — keep it that way (SPEC test 9)."""
    config = document.config
    alias = config.aliases.get(alias_name)
    if alias is None:
        # the candidate config cannot serve this alias — record the miss
        return _event(
            request_id=str(uuid.uuid4()),
            alias=alias_name,
            mode="missing",
            target=None,
            attempts=[],
            http_status=0,
            outcome="shadow",
            started_at=time.perf_counter(),
            usage=None,
            streaming=False,
            fallback=False,
            identity=identity,
            config_version=document.version,
        )
    eligible, _exclusions = cost_eligible(alias, ordered_targets(config, alias_name))
    selected: Target | None = None
    for target in eligible:
        if store.active_for(target.provider_id, target.credential_id, target.model) is not None:
            continue
        if not os.environ.get(target.api_key_env, "").strip():
            continue
        selected = target
        break
    return _event(
        request_id=str(uuid.uuid4()),
        alias=alias_name,
        mode=alias.mode,
        target=selected,
        attempts=[],
        http_status=0,
        outcome="shadow",
        started_at=time.perf_counter(),
        usage=None,
        streaming=False,
        fallback=False,
        identity=identity,
        config_version=document.version,
    )


async def dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    identity_name: str | None = None,
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
    ordered = [f"{t.provider_id}/{t.credential_id}/{t.model}" for t in targets]
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
            attempts.append(_attempt(target, "missing_credentials", attempt_started, fallback=index > 0))
            continue

        attempted += 1
        request = build_upstream_request(client, target, body, api_key)
        try:
            response = await client.send(request, stream=streaming)
        except httpx.HTTPError:
            attempts.append(
                _attempt(target, "transport_error", attempt_started, fallback=index > 0, cooldown_scope="model")
            )
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
            # a 429 applies target.quota_scope to the store; record that exact
            # scope on the attempt so operators can see what was cooled down
            applied_scope = target.quota_scope if response.status_code == 429 else None
            attempts.append(
                _attempt(
                    target,
                    "retryable_status",
                    attempt_started,
                    response.status_code,
                    fallback=index > 0,
                    cooldown_scope=applied_scope,
                )
            )
            if response.status_code == 429:
                retry_after = retry_after_seconds(response)
                duration = (
                    min(retry_after, target.quota_cooldown_seconds)
                    if retry_after is not None
                    else target.quota_cooldown_seconds
                )
                store.apply(
                    scope=target.quota_scope,  # model by default; credential = account-wide exhaustion
                    provider=target.provider_id,
                    credential=target.credential_id,
                    model=target.model if target.quota_scope == "model" else None,
                    reason="quota_429",
                    duration_seconds=duration,
                )
            await response.aclose()
            continue

        used_fallback = index > 0
        final_attempt = _attempt(target, "response", attempt_started, response.status_code, fallback=used_fallback)
        attempts.append(final_attempt)
        metadata = {
            "request_id": request_id,
            "alias": alias_name,
            "mode": alias.mode,
            "identity": identity_name,
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
                            identity=identity_name,
                            config_version=document.version,
                            candidates=ordered,
                            exclusions=list(exclusions),
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
        except (json.JSONDecodeError, UnicodeDecodeError):
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
                    identity=identity_name,
                    config_version=document.version,
                    candidates=ordered,
                    exclusions=list(exclusions),
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
                identity=identity_name,
                config_version=document.version,
                candidates=ordered,
                exclusions=list(exclusions),
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
            identity=identity_name,
            config_version=document.version,
            candidates=ordered,
            exclusions=list(exclusions),
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


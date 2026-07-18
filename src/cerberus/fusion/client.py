"""Fan out a fusion-mode request to the bundled worker and re-emit its telemetry.

Cerberus holds the fusion POLICY; the worker (fusion_backend fork) EXECUTES panel+judge.
This module is the seam: it calls the worker, applies the alias's partial-failure
policy, and re-emits each panel seat + the judge into the unified routing stream.
A worker outage fails only fusion aliases (fail closed), never dispatch/free.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi.responses import JSONResponse

from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias
from cerberus.telemetry import RoutingAttempt, RoutingEvent, TelemetryEmitter
from cerberus.registry.schema import FusionWorkerConfig

WORKER_MODEL = "fusion_backend"


class FusionWorkerClient:
    """Thin async client for the internal fusion worker."""

    def __init__(
        self,
        config: FusionWorkerConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = str(config.endpoint).rstrip("/") if config.endpoint is not None else None
        self._token_env = config.bearer_token_env
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=config.connect_timeout_seconds,
        )

    @property
    def configured(self) -> bool:
        return self._endpoint is not None

    async def complete(self, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        assert self._endpoint is not None
        headers = {}
        if self._token_env:
            token = os.environ.get(self._token_env, "")
            if token:
                headers["authorization"] = f"Bearer {token}"
        response = await self._client.post(
            f"{self._endpoint}/v1/chat/completions",
            json={**body, "model": WORKER_MODEL},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _panel_attempts(report: dict[str, Any]) -> tuple[list[RoutingAttempt], int]:
    """Build one attempt per panel seat plus the judge; return (attempts, failed_count)."""

    attempts: list[RoutingAttempt] = []
    failed = 0
    for seat in report.get("panel", []):
        ok = bool(seat.get("ok"))
        if not ok:
            failed += 1
        attempts.append(
            RoutingAttempt(
                provider=str(seat.get("label", "panel")),
                pool="panel",
                model=str(seat.get("model") or seat.get("label") or ""),
                used_fallback=False,  # panel seats run in parallel, not as fallbacks
                outcome="response" if ok else "transport_error",
                latency_ms=0.0,
                http_status=seat.get("status_code"),
            )
        )
    judge = report.get("judge") or {}
    attempts.append(
        RoutingAttempt(
            provider="judge",
            pool="judge",
            model=str(judge.get("model") or ""),
            used_fallback=False,
            outcome="response",
            latency_ms=0.0,
        )
    )
    return attempts, failed


async def fusion_dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    alias: Alias,
    identity_name: str | None,
    document: ConfigDocument,
    worker: FusionWorkerClient,
    telemetry: TelemetryEmitter,
) -> JSONResponse:
    request_id = str(uuid.uuid4())
    started_at = time.perf_counter()
    policy = alias.fusion
    assert policy is not None
    judge = policy.judge
    candidates = [f"{c.provider}/{c.model}" for c in alias.candidates]

    def emit(*, outcome, http_status, attempts, usage, failed_seats=0):
        telemetry.emit(
            RoutingEvent(
                request_id=request_id,
                alias=alias_name,
                provider=judge.provider,
                mode="fusion",
                model=judge.model,
                used_fallback=False,
                credential=judge.credential,
                candidates=candidates,
                exclusions=[{"reason": "panel_member_failed", "count": failed_seats}] if failed_seats else [],
                attempts=attempts,
                http_status=http_status,
                outcome=outcome,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                token_usage=usage,
                timestamp=datetime.now(timezone.utc),
                streaming=False,
                identity=identity_name,
                config_version=document.version,
            )
        )

    if not worker.configured:
        emit(outcome="fusion_unavailable", http_status=503, attempts=[], usage=None)
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "Fusion worker not configured", "request_id": request_id}},
        )

    # fusion runs non-streaming so the worker's panel/judge report is available to
    # re-emit; a streaming fusion surface is a later increment.
    call_body = {k: v for k, v in body.items() if k != "stream"}
    try:
        payload = await worker.complete(call_body, timeout=float(policy.timeout_seconds))
    except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
        status = 502
        outcome = "upstream_error"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
        elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
            outcome = "fusion_unavailable"
            status = 503
        emit(outcome=outcome, http_status=status, attempts=[], usage=None)
        return JSONResponse(
            status_code=status,
            content={"error": {"message": "Fusion worker request failed", "request_id": request_id}},
        )

    report = payload.pop("fusion_backend", {}) if isinstance(payload, dict) else {}
    attempts, failed_seats = _panel_attempts(report)
    usage = payload.get("usage") if isinstance(payload, dict) else None

    if failed_seats and policy.on_partial_failure == "fail":
        emit(outcome="upstream_error", http_status=502, attempts=attempts, usage=usage, failed_seats=failed_seats)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"Fusion panel had {failed_seats} failed member(s); alias policy is fail",
                    "request_id": request_id,
                }
            },
        )

    emit(outcome="success", http_status=200, attempts=attempts, usage=usage, failed_seats=failed_seats)
    if isinstance(payload, dict):
        payload = {
            **payload,
            "cerberus": {
                "request_id": request_id,
                "alias": alias_name,
                "mode": "fusion",
                "identity": identity_name,
                "panel": candidates,
                "judge": f"{judge.provider}/{judge.model}",
                "config_version": document.version,
            },
        }
    return JSONResponse(content=payload, status_code=200, headers={"x-request-id": request_id})

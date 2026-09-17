"""Route a fusion-mode request through the configured Fusion backend.

Cerberus holds the fusion POLICY (alias authorization, panel membership, analyst,
cost tier, deadline); a managed backend performs the deliberation. This module
is the seam: it resolves the alias into a backend request, calls the backend,
emits one unified routing event, and wraps the completion in the Cerberus
response contract. A backend outage fails only fusion aliases (fail closed),
never dispatch/free.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

from cerberus.fusion.backend import FusionBackend, FusionError, FusionRequest
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias, CerberusConfig
from cerberus.telemetry import RoutingAttempt, RoutingEvent, TelemetryEmitter

# Caller-supplied keys a fusion alias cannot honor: the backend owns the tool
# surface during deliberation, so accepting them would silently drop or reroute
# the caller's intent. Rejected up front rather than forwarded.
_REJECTED_CALLER_KEYS = ("tools", "tool_choice", "plugins")


def fusion_aliases(config: CerberusConfig) -> list[str]:
    return [name for name, alias in config.aliases.items() if alias.mode == "fusion"]


def fusion_status(config: CerberusConfig) -> dict[str, Any]:
    """Truthful readiness for the admin surface: an alias counts as configured
    only when its backend credential is actually present in the environment."""

    aliases = fusion_aliases(config)
    ready = [name for name in aliases if _credential_for(config, config.aliases[name])[1]]
    backends = sorted({config.aliases[name].fusion.backend for name in aliases if config.aliases[name].fusion})
    return {
        "state": "configured" if aliases and len(ready) == len(aliases) else "not_configured",
        "backends": backends,
        "aliases": aliases,
    }


def fusion_readiness(
    config: CerberusConfig,
    alias: Alias,
    *,
    backend_names: Collection[str],
    control_plane: Any | None,
) -> dict[str, bool]:
    """Whether this fusion alias could deliberate right now, by its own gates.

    Fusion does not run its panel through the failover loop: it resolves one
    backend, one credential and one provider, then sends a single deliberation
    request. Those three gates are the whole of its availability, and
    ``fusion_dispatch`` refuses with 503 when any fails. ``/admin/routes``
    reports the same three from here, so the console cannot claim a readiness
    the dispatch path would not honour.
    """

    assert alias.fusion is not None
    policy = alias.fusion
    _, api_key = _credential_for(config, alias)
    backend_present = policy.backend in backend_names
    credential_present = bool(api_key)
    provider_available = control_plane is None or control_plane.provider_available(policy.judge.provider)
    return {
        "backend_present": backend_present,
        "credential_present": credential_present,
        "provider_available": provider_available,
        "available": backend_present and credential_present and provider_available,
    }


def _credential_for(config: CerberusConfig, alias: Alias) -> tuple[str, str]:
    assert alias.fusion is not None
    judge = alias.fusion.judge
    provider = config.providers[judge.provider]
    env_name = provider.credentials[judge.credential].api_key_env
    return str(provider.base_url), os.environ.get(env_name, "")


async def fusion_dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    alias: Alias,
    identity_name: str | None,
    document: ConfigDocument,
    backends: dict[str, FusionBackend],
    telemetry: TelemetryEmitter,
    control_plane: Any | None = None,
) -> JSONResponse:
    request_id = str(uuid.uuid4())
    started_at = time.perf_counter()
    policy = alias.fusion
    assert policy is not None
    judge = policy.judge
    candidates = [f"{c.provider}/{c.model}" for c in alias.candidates]
    panel_models = [c.model for c in alias.candidates]
    fusion_record: dict[str, Any] = {
        "backend": policy.backend,
        "panel": candidates,
        "analyst": f"{judge.provider}/{judge.model}",
        "returned_model": None,
        "generation_id": None,
        "metadata": None,
    }

    def emit(*, outcome, http_status, attempts, usage):
        telemetry.emit(
            RoutingEvent(
                request_id=request_id,
                alias=alias_name,
                provider=judge.provider,
                mode="fusion",
                model=fusion_record["returned_model"] or judge.model,
                used_fallback=False,
                credential=judge.credential,
                candidates=candidates,
                exclusions=[],
                attempts=attempts,
                http_status=http_status,
                outcome=outcome,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                token_usage=usage,
                reported_cost=(
                    float(usage["cost"])
                    if isinstance(usage, dict)
                    and isinstance(usage.get("cost"), (int, float))
                    and not isinstance(usage.get("cost"), bool)
                    and usage["cost"] >= 0
                    else None
                ),
                timestamp=datetime.now(timezone.utc),
                streaming=False,
                identity=identity_name,
                config_version=document.version,
                config_checksum=document.checksum,
                fusion=dict(fusion_record),
            )
        )

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"message": message, "request_id": request_id}})

    rejected = [key for key in _REJECTED_CALLER_KEYS if key in body]
    if rejected:
        emit(outcome="upstream_error", http_status=400, attempts=[], usage=None)
        return error(400, f"fusion alias does not accept caller-supplied {', '.join(rejected)}")

    backend = backends.get(policy.backend)
    base_url, api_key = _credential_for(document.config, alias)
    readiness = fusion_readiness(
        document.config, alias, backend_names=backends.keys(), control_plane=control_plane
    )
    if not readiness["available"]:
        emit(outcome="fusion_unavailable", http_status=503, attempts=[], usage=None)
        return error(503, "Fusion backend not configured")

    # Fusion runs non-streaming: the deliberation completes as one upstream call
    # and Cerberus reports one unified event for it.
    request = FusionRequest(
        body={k: v for k, v in body.items() if k != "stream"},
        panel_models=panel_models,
        analyst_model=judge.model,
        outer_model=policy.outer_model.model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(policy.timeout_seconds),
    )

    def attempt(outcome, latency_ms, http_status=None) -> RoutingAttempt:
        # exactly one upstream call: the backend's deliberation request
        return RoutingAttempt(
            provider=judge.provider,
            pool="fusion",
            model=fusion_record["returned_model"] or backend.name,
            used_fallback=False,
            outcome=outcome,
            latency_ms=latency_ms,
            http_status=http_status,
        )

    try:
        result = await backend.execute(request)
    except FusionError as exc:
        emit(
            outcome=exc.outcome,
            http_status=exc.http_status,
            attempts=[attempt(exc.attempt_outcome, exc.latency_ms, exc.http_status)],
            usage=None,
        )
        return error(exc.http_status, str(exc))

    fusion_record.update(
        returned_model=result.returned_model,
        generation_id=result.generation_id,
        metadata=result.metadata or None,
    )
    emit(
        outcome="success",
        http_status=200,
        attempts=[attempt("response", result.latency_ms, result.http_status)],
        usage=result.usage,
    )
    payload = {
        **result.payload,
        "cerberus": {
            "request_id": request_id,
            "alias": alias_name,
            "mode": "fusion",
            "identity": identity_name,
            "backend": policy.backend,
            "panel": candidates,
            "judge": f"{judge.provider}/{judge.model}",
            "outer": f"{policy.outer_model.provider}/{policy.outer_model.model}",
            "config_version": document.version,
            "config_checksum": document.checksum,
        },
    }
    return JSONResponse(content=payload, status_code=200, headers={"x-request-id": request_id})

"""Route a jev-router request: Cerberus policy first, Jev's choice second.

Cerberus resolves the alias's pool and filters it (cost, health, cooldown,
credential — ``router/pool.py``) before Jev sees anything. The survivors go to
the hosted Jev Router as exact slugs; Jev picks one. Cerberus then checks the
router's own account of what it did against the pool it sent, and withholds any
response that left it. A backend outage fails only jev-router aliases, never
dispatch, free or fusion.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

# cerberus.router before cerberus.egress: egress.client imports router.engine, and
# the router package imports egress.client in turn, so egress first is a cycle.
from cerberus.router.engine import Target
from cerberus.router.pool import PoolMember, resolve_pool
from cerberus.egress.client import reported_cost, token_usage
from cerberus.jev_router.backend import (
    JEV_ROUTER_MODEL,
    JevRouterBackend,
    JevRouterError,
    JevRouterRequest,
    JevRouterResult,
    slug_matches,
)
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias, JevRouterPolicy
from cerberus.telemetry import RoutingAttempt, RoutingEvent, TelemetryEmitter

# What a caller may set on a jev-router request. This is an allowlist, not a
# denylist: anything that decides where a request runs (model fallback lists,
# route, plugins, presets, provider routing, server tools) is Cerberus's to
# construct, and anything not listed here is refused rather than forwarded, so
# a field OpenRouter adds tomorrow cannot widen the pool. reasoning_effort and
# reasoning are absent on purpose: Jev chooses the effort per request, and
# overriding it silently in either direction would hide the substitution.
_CALLER_FIELDS = frozenset(
    {
        "messages",
        "model",  # the alias; replaced by the router model
        "stream",  # removed; jev-router is not streamed
        "stream_options",
        "temperature",
        "top_p",
        "n",
        "stop",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "user",
        "response_format",
        "tools",  # function tools only; see caller_field_violations
        "tool_choice",
        "parallel_tool_calls",
        "functions",
        "function_call",
    }
)
_TOOL_CHOICE_MODES = frozenset({"none", "auto", "required"})


def caller_field_violations(body: dict[str, Any]) -> list[str]:
    """The fields a jev-router alias refuses in this body; empty when acceptable.

    Function tools run on the caller's side and are accepted. Any other tool
    type is a server tool that executes upstream under Cerberus's credential —
    ``openrouter:fusion`` alone names a whole panel — so it is refused.
    """

    found = sorted(key for key in body if key not in _CALLER_FIELDS)
    tools = body.get("tools")
    if tools is not None and not (
        isinstance(tools, list) and all(isinstance(t, dict) and t.get("type") == "function" for t in tools)
    ):
        found.append("tools")
    choice = body.get("tool_choice")
    if choice is not None and not (
        (isinstance(choice, str) and choice in _TOOL_CHOICE_MODES)
        or (isinstance(choice, dict) and choice.get("type") == "function")
    ):
        found.append("tool_choice")
    return found


def jev_router_readiness(
    members: list[PoolMember], policy: JevRouterPolicy, *, backend_names: Collection[str]
) -> dict[str, Any]:
    """Whether this alias could route right now, by its own two gates.

    A routed request needs its backend and at least one pool member that
    survived Cerberus's filters. ``jev_router_dispatch`` refuses with 503 when
    either fails, and ``/admin/routes`` reports the same answer from here.
    """

    backend_present = policy.backend in backend_names
    pool_available = sum(1 for member in members if member.exclusion is None)
    return {
        "backend_present": backend_present,
        "pool_available": pool_available,
        "pool_size": len(members),
        "available": backend_present and pool_available > 0,
    }


def served_target(model: str, pool: list[Target]) -> Target | None:
    """The pool member a model the router named belongs to, or None."""

    return next((target for target in pool if slug_matches(model, target.model)), None)


def pool_violation(result: JevRouterResult, pool: list[Target]) -> str | None:
    """Why a routed response left the pool Cerberus sent, or None if it stayed.

    Fails closed. A response is accepted only when the router's own decision is
    present, says it applied the include list, and every model it names — the
    one that served and any it lined up behind it — is in the pool.
    """

    decision = result.decision
    if decision is None:
        return "decision_unverifiable"
    if decision.list_fallback is not None:
        # "models_ignored" means OpenRouter routed over its whole pool
        return "models_ignored" if decision.list_fallback == "models_ignored" else "list_fallback"
    if result.returned_model is None:
        return "returned_model_unknown"
    if served_target(result.returned_model, pool) is None:
        return "returned_model_outside_pool"
    if any(served_target(model, pool) is None for model in decision.resolved_models or []):
        return "resolved_model_outside_pool"
    return None


async def jev_router_dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    alias: Alias,
    identity_name: str | None,
    document: ConfigDocument,
    backends: dict[str, JevRouterBackend],
    store: Any,
    telemetry: TelemetryEmitter,
    control_plane: Any | None = None,
) -> JSONResponse:
    request_id = str(uuid.uuid4())
    started_at = time.perf_counter()
    policy = alias.jev_router
    assert policy is not None

    members = resolve_pool(
        document.config, alias_name, allow_paid=policy.allow_paid_pool, control_plane=control_plane, store=store
    )
    anchor = members[0].target  # every member shares this provider and credential
    pool = [member.target for member in members if member.exclusion is None]
    candidates = [f"{member.target.provider_id}/{member.target.model}" for member in members]
    exclusions = [
        {**member.target.describe(), **member.exclusion.telemetry()}
        for member in members
        if member.exclusion is not None
    ]
    record: dict[str, Any] = {
        "backend": policy.backend,
        "router_model": JEV_ROUTER_MODEL,
        "pool": [target.model for target in pool],
        "returned_model": None,
        "generation_id": None,
        "decision": None,
        "violation": None,
        "metadata": None,
    }

    def emit(*, outcome, http_status, attempts, payload=None, served=None):
        telemetry.emit(
            RoutingEvent(
                request_id=request_id,
                alias=alias_name,
                provider=anchor.provider_id,
                mode=alias.mode,
                model=record["returned_model"] or JEV_ROUTER_MODEL,
                used_fallback=False,
                credential=anchor.credential_id,
                cost_tier=served.cost_tier if served is not None else None,
                reported_cost=reported_cost(payload) if payload is not None else None,
                candidates=candidates,
                exclusions=exclusions,
                attempts=attempts,
                http_status=http_status,
                outcome=outcome,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                token_usage=token_usage(payload) if payload is not None else None,
                timestamp=datetime.now(timezone.utc),
                streaming=False,
                identity=identity_name,
                config_version=document.version,
                config_checksum=document.checksum,
                jev_router=dict(record),
            )
        )

    def error(status: int, message: str, **extra: Any) -> JSONResponse:
        return JSONResponse(
            status_code=status, content={"error": {"message": message, "request_id": request_id, **extra}}
        )

    def attempt(outcome, latency_ms, http_status=None) -> RoutingAttempt:
        # exactly one upstream call: the routed request
        return RoutingAttempt(
            provider=f"{anchor.provider_id}/{anchor.credential_id}",
            pool="jev-router",
            model=record["returned_model"] or JEV_ROUTER_MODEL,
            used_fallback=False,
            outcome=outcome,
            latency_ms=latency_ms,
            http_status=http_status,
        )

    rejected = caller_field_violations(body)
    if rejected:
        emit(outcome="invalid_request", http_status=400, attempts=[])
        return error(
            400,
            f"jev-router alias does not accept caller-supplied {', '.join(rejected)}",
            reason="caller_field_not_allowed",
        )

    readiness = jev_router_readiness(members, policy, backend_names=backends.keys())
    if not readiness["backend_present"]:
        emit(outcome="jev_router_unavailable", http_status=503, attempts=[])
        return error(503, "Jev Router backend not configured")
    if not pool:
        # Never send an empty or partial-by-accident list: OpenRouter answers a
        # list that matches nothing by routing over its whole pool.
        emit(outcome="routing_exhausted", http_status=503, attempts=[])
        return error(503, "No pool model available", exclusions=exclusions)

    # Non-streaming: the router's decision has to be checked against the pool
    # before any of the answer reaches the caller.
    request = JevRouterRequest(
        body={k: v for k, v in body.items() if k != "stream"},
        pool_models=[target.model for target in pool],
        base_url=anchor.base_url,
        api_key=os.environ.get(anchor.api_key_env, "").strip(),
        timeout_seconds=float(policy.timeout_seconds),
    )
    backend = backends[policy.backend]
    try:
        result = await backend.execute(request)
    except JevRouterError as exc:
        emit(
            outcome=exc.outcome,
            http_status=exc.http_status,
            attempts=[attempt(exc.attempt_outcome, exc.latency_ms, exc.upstream_status)],
        )
        return error(exc.http_status, str(exc))

    violation = pool_violation(result, pool)
    served = served_target(result.returned_model, pool) if result.returned_model is not None else None
    record.update(
        returned_model=result.returned_model,
        generation_id=result.generation_id,
        decision=result.decision.record() if result.decision is not None else None,
        violation=violation,
        metadata=result.metadata or None,
    )
    if violation is not None:
        # The call ran and is billed, so its usage and cost are still recorded.
        # The answer is withheld: it came from outside the pool Cerberus sent,
        # or with no evidence that it did not.
        emit(
            outcome="out_of_policy",
            http_status=502,
            attempts=[attempt("out_of_policy", result.latency_ms, result.http_status)],
            payload=result.payload,
        )
        return error(502, f"Jev Router response withheld: {violation}", reason=violation)

    emit(
        outcome="success",
        http_status=200,
        attempts=[attempt("response", result.latency_ms, result.http_status)],
        payload=result.payload,
        served=served,
    )
    assert served is not None and result.decision is not None
    payload = {
        **result.payload,
        "cerberus": {
            "request_id": request_id,
            "alias": alias_name,
            "mode": alias.mode,
            "identity": identity_name,
            "backend": policy.backend,
            "router": JEV_ROUTER_MODEL,
            "pool": [f"{target.provider_id}/{target.model}" for target in pool],
            "model": f"{served.provider_id}/{result.returned_model}",
            "decision": result.decision.record(),
            "config_version": document.version,
            "config_checksum": document.checksum,
        },
    }
    return JSONResponse(content=payload, status_code=200, headers={"x-request-id": request_id})

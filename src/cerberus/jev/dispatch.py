"""Route a jev request: Cerberus decides what may run, Jev decides what should.

    alias candidates (any provider)
      → cost, health, cooldown, credential       router/pool.py
      → distinct models offered under opaque ids  jev/state.py
      → one decision: which is cheapest-sufficient jev/backend.py
      → chosen model first, rest in config order  DispatchPlan
      → the ordinary failover loop executes       router/dispatch.py

Jev never executes anything and never widens the pool: its answer can only
reorder models policy already admitted. Any failure to get a usable answer —
no credential, timeout, transport or HTTP error, an unreadable response, a
choice that is not an offered id — runs the request in configured order and
records why. With one model or none left to choose from, no decision is asked.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from typing import Any

# cerberus.router before anything importing cerberus.egress: egress.client
# imports router.engine, and the router package imports egress.client in turn
from cerberus.router.dispatch import DispatchPlan, dispatch
from cerberus.router.pool import PoolMember, resolve_pool
from cerberus.jev.backend import DecisionBackend, DecisionError, DecisionRequest
from cerberus.jev.state import Option, decision_state, options_for
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias, CerberusConfig

COST_REFUSAL = "paid_pool_prohibited"


def decider_readiness(
    config: CerberusConfig,
    alias: Alias,
    *,
    backend_names: Collection[str],
    control_plane: Any | None,
) -> dict[str, bool]:
    """Whether a decision could be asked right now, by its own three gates.

    Not whether the alias can route: a jev alias still serves, in configured
    order, while no decision can be had. ``/admin/routes`` reports both.
    """

    assert alias.jev is not None
    decider = alias.jev.decider
    backend_present = alias.jev.backend in backend_names
    credential_present = bool(_decider_key(config, alias))
    provider_available = control_plane is None or control_plane.provider_available(decider.provider)
    return {
        "backend_present": backend_present,
        "credential_present": credential_present,
        "provider_available": provider_available,
        "decides": backend_present and credential_present and provider_available,
    }


def _decider_key(config: CerberusConfig, alias: Alias) -> str:
    assert alias.jev is not None
    decider = alias.jev.decider
    env_name = config.providers[decider.provider].credentials[decider.credential].api_key_env
    return os.environ.get(env_name, "").strip()


def _cost_refused(member: PoolMember) -> bool:
    return member.exclusion is not None and member.exclusion.reason == COST_REFUSAL


async def jev_dispatch(
    *,
    body: dict[str, Any],
    alias_name: str,
    alias: Alias,
    identity_name: str | None,
    document: ConfigDocument,
    backends: dict[str, DecisionBackend],
    store: Any,
    client: Any,
    telemetry: Any,
    control_plane: Any | None = None,
):
    policy = alias.jev
    assert policy is not None
    config = document.config

    members = resolve_pool(
        config, alias_name, allow_paid=policy.allow_paid_pool, control_plane=control_plane, store=store
    )
    # The loop re-applies health, cooldown and credential itself, so the plan
    # carries every cost-admitted target; only what is attemptable right now is
    # offered to the decision, so it cannot pick a model the loop would skip.
    admitted = [member.target for member in members if not _cost_refused(member)]
    cost_exclusions = [
        {**member.target.describe(), **member.exclusion.telemetry()}
        for member in members
        if _cost_refused(member) and member.exclusion is not None
    ]
    options = options_for([member.target for member in members if member.exclusion is None])

    record: dict[str, Any] = {
        "decider": policy.backend,
        "model": policy.model,
        "input": policy.input,
        "options": [option.label for option in options],
        "decision": None,  # chosen | skipped | fallback
        "reason": None,
        "choice": None,
        "confidence": None,
        "latency_ms": None,
        "http_status": None,
        "usage": None,
    }
    chosen = await _decide(body, options, config, alias, policy, backends, control_plane, record)

    if chosen is None:
        order = admitted
    else:
        first = [t for t in admitted if (t.provider_id, t.model) == (chosen.provider, chosen.model)]
        order = first + [t for t in admitted if (t.provider_id, t.model) != (chosen.provider, chosen.model)]

    return await dispatch(
        body=body,
        alias_name=alias_name,
        identity_name=identity_name,
        document=document,
        store=store,
        client=client,
        telemetry=telemetry,
        control_plane=control_plane,
        plan=DispatchPlan(targets=order, exclusions=cost_exclusions, jev=record),
    )


async def _decide(
    body: dict[str, Any],
    options: list[Option],
    config: CerberusConfig,
    alias: Alias,
    policy: Any,
    backends: dict[str, DecisionBackend],
    control_plane: Any | None,
    record: dict[str, Any],
) -> Option | None:
    """The option Jev chose, or None with ``record`` saying why there is none."""

    def without(decision: str, reason: str) -> None:
        record.update(decision=decision, reason=reason)

    if len(options) <= 1:
        # nothing to choose between: no request text leaves for a decision
        without("skipped", "single_option" if options else "no_option")
        return None
    readiness = decider_readiness(config, alias, backend_names=backends.keys(), control_plane=control_plane)
    if not readiness["backend_present"]:
        without("fallback", "backend_missing")
        return None
    if not readiness["credential_present"]:
        without("fallback", "credential_missing")
        return None
    if not readiness["provider_available"]:
        without("fallback", "decider_unavailable")
        return None

    request = DecisionRequest(
        endpoint=str(policy.endpoint),
        api_key=_decider_key(config, alias),
        model=policy.model,
        state=decision_state(body, options, config, input_mode=policy.input, max_chars=policy.max_input_chars),
        options=[option.id for option in options],
        timeout_seconds=float(policy.timeout_seconds),
    )
    try:
        decision = await backends[policy.backend].decide(request)
    except DecisionError as exc:
        record.update(latency_ms=round(exc.latency_ms, 3), http_status=exc.http_status)
        without("fallback", exc.reason)
        return None

    record.update(
        latency_ms=round(decision.latency_ms, 3),
        http_status=decision.http_status,
        usage=decision.usage,
        confidence=decision.confidence,
    )
    chosen = next((option for option in options if option.id == decision.choice), None)
    if chosen is None:
        # absent, or not an id Cerberus offered: never guessed at
        without("fallback", "choice_outside_pool" if decision.choice is not None else "no_choice")
        record["confidence"] = None
        return None
    record.update(decision="chosen", choice=chosen.label)
    return chosen

"""Route a jev request: Cerberus decides what may run, Jev decides what should.

    alias candidates (any provider)
      → cost, health, cooldown, credential       router/pool.py
      → distinct models offered under opaque ids  jev/state.py
      → one decision: which is cheapest-sufficient jev/backend.py
        (and, by policy, how much reasoning effort)
      → chosen model first, rest in config order  DispatchPlan
      → the ordinary failover loop executes       router/dispatch.py

Jev never executes anything and never widens the pool: its answer can only
reorder models policy already admitted. Any failure to get a usable answer —
no credential, timeout, transport or HTTP error, an unreadable response, a
choice that is not an offered id — runs the request in configured order and
records why. With one model or none left to choose from, no model is asked
for; with no effort question either, no decision call is made at all.

The effort answer, when policy asks for one, replaces the configured
reasoning_effort of every planned candidate that sets one, and of no other. The
egress keeps its rule that a caller's own stated effort wins unless the
candidate sets reasoning_effort_override.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import replace
from typing import Any

# cerberus.router before anything importing cerberus.egress: egress.client
# imports router.engine, and the router package imports egress.client in turn
from cerberus.router.dispatch import DispatchPlan, dispatch
from cerberus.router.engine import Target
from cerberus.router.pool import PoolMember, resolve_pool
from cerberus.jev.backend import (
    EFFORT_QUESTION,
    EFFORT_QUESTION_ID,
    ROUTE_QUESTION,
    ROUTE_QUESTION_ID,
    DecisionBackend,
    DecisionError,
    DecisionRequest,
    Question,
)
from cerberus.jev.state import Option, decision_state, options_for
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias, CerberusConfig, JevPolicy

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
    attemptable = [member.target for member in members if member.exclusion is None]
    options = options_for(attemptable)

    record: dict[str, Any] = {
        "decider": policy.backend,
        "model": policy.model,
        "input": policy.input,
        "options": [option.label for option in options],
        "decision": None,  # chosen | skipped | fallback
        "reason": None,
        "choice": None,
        "confidence": None,
        "effort": None,  # the reasoning effort Jev chose and Cerberus applied
        "effort_confidence": None,
        "effort_reason": None,  # why no effort was applied, when none was
        "latency_ms": None,
        "http_status": None,
        "usage": None,
    }
    chosen, effort = await _decide(body, options, attemptable, config, alias, policy, backends, control_plane, record)

    if chosen is None:
        order = admitted
    else:
        first = [t for t in admitted if (t.provider_id, t.model) == (chosen.provider, chosen.model)]
        order = first + [t for t in admitted if (t.provider_id, t.model) != (chosen.provider, chosen.model)]
    if effort is not None:
        # only where the route already sets an effort: a route without one may
        # not accept the parameter at all
        order = [replace(t, reasoning_effort=effort) if t.reasoning_effort is not None else t for t in order]

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
    attemptable: list[Target],
    config: CerberusConfig,
    alias: Alias,
    policy: JevPolicy,
    backends: dict[str, DecisionBackend],
    control_plane: Any | None,
    record: dict[str, Any],
) -> tuple[Option | None, str | None]:
    """The model and the effort Jev chose, each None with ``record`` saying why.

    The model is asked for only with two or more to choose between, and the
    effort only when policy lists efforts and an attemptable candidate sets
    one for it to replace. With neither to ask, no call is made and no request
    text leaves.
    """

    ask_route = len(options) > 1
    ask_effort = policy.reasoning_efforts is not None and any(t.reasoning_effort is not None for t in attemptable)
    if not ask_route:
        record.update(decision="skipped", reason="single_option" if options else "no_option")
    if not ask_effort:
        record["effort_reason"] = "not_requested" if policy.reasoning_efforts is None else "not_applicable"
    if not (ask_route or ask_effort):
        return None, None

    def failed(reason: str) -> tuple[None, None]:
        if ask_route:
            record.update(decision="fallback", reason=reason)
        if ask_effort:
            record["effort_reason"] = reason
        return None, None

    readiness = decider_readiness(config, alias, backend_names=backends.keys(), control_plane=control_plane)
    if not readiness["backend_present"]:
        return failed("backend_missing")
    if not readiness["credential_present"]:
        return failed("credential_missing")
    if not readiness["provider_available"]:
        return failed("decider_unavailable")

    questions = []
    if ask_route:
        questions.append(Question(ROUTE_QUESTION_ID, ROUTE_QUESTION, [option.id for option in options]))
    if ask_effort:
        assert policy.reasoning_efforts is not None
        questions.append(Question(EFFORT_QUESTION_ID, EFFORT_QUESTION, list(policy.reasoning_efforts)))
    request = DecisionRequest(
        endpoint=str(policy.endpoint),
        api_key=_decider_key(config, alias),
        model=policy.model,
        state=decision_state(body, options, config, input_mode=policy.input, max_chars=policy.max_input_chars),
        questions=questions,
        timeout_seconds=float(policy.timeout_seconds),
    )
    try:
        decision = await backends[policy.backend].decide(request)
    except DecisionError as exc:
        record.update(latency_ms=round(exc.latency_ms, 3), http_status=exc.http_status)
        return failed(exc.reason)
    record.update(latency_ms=round(decision.latency_ms, 3), http_status=decision.http_status, usage=decision.usage)

    # Each answer is vetted on its own and never guessed at: it must be exactly
    # an option Cerberus offered for that question.
    chosen = None
    if ask_route:
        answer = decision.answers[ROUTE_QUESTION_ID]
        chosen = next((option for option in options if option.id == answer.choice), None)
        if chosen is None:
            record.update(decision="fallback", reason="choice_outside_pool" if answer.choice is not None else "no_choice")
        else:
            record.update(decision="chosen", choice=chosen.label, confidence=answer.confidence)
    effort = None
    if ask_effort:
        answer = decision.answers[EFFORT_QUESTION_ID]
        if answer.choice in (policy.reasoning_efforts or []):
            effort = answer.choice
            record.update(effort=effort, effort_confidence=answer.confidence)
        else:
            record["effort_reason"] = "choice_outside_options" if answer.choice is not None else "no_choice"
    return chosen, effort

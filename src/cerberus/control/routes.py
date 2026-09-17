"""The console's route projection — the router's own answers, shaped for reading.

The Routes screen asks, per alias: where can this alias go, what policy applies,
and why is a particular path eligible or unavailable right now. Every one of
those is a routing decision, so none of them are made here and none may be made
in a browser. This module calls the deciders and arranges what they return:

    ordered_targets()      the ordered policy decision for the alias
    cost_eligible()        cost-tier policy — cost_tier_guard, paid_fallback_prohibited
    runtime_exclusion()    health, then cooldown, then credential presence
    authorization_error()  whether a configured identity may use the alias
    fusion_readiness()     backend, credential and provider gates for fusion

Fusion is projected differently on purpose. Its panel never enters the failover
loop — Cerberus resolves one backend, one credential and one provider and sends
a single deliberation request — so panel members carry no eligibility, standby
or cooldown state. Calling them "eligible" would describe a loop that does not
run. They are a service chain with one readiness verdict over the whole alias.

Read-only: no upstream connection, no cooldown applied, no lifecycle state, and
no secret value or credential locator — a path names its credential, never where
that credential is read from.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from cerberus.fusion.dispatch import fusion_readiness
from cerberus.identity.auth import IdentityContext, authorization_error
from cerberus.registry.loader import ConfigDocument
from cerberus.registry.schema import Alias, CerberusConfig, Candidate
from cerberus.router.availability import runtime_exclusion
from cerberus.router.engine import Target, cost_eligible, ordered_targets

# a path is eligible when the router would attempt it first, standby when it is
# attemptable but a later preference, excluded when the router would skip it
# before making any upstream request
STATE_ELIGIBLE = "eligible"
STATE_STANDBY = "standby"
STATE_EXCLUDED = "excluded"


def _member(candidate: Candidate, order: int) -> dict[str, Any]:
    return {
        "order": order,
        "provider": candidate.provider,
        "model": candidate.model,
        "credential_ref": candidate.credential,
    }


def _participant(candidate: Candidate) -> dict[str, Any]:
    return {
        "provider": candidate.provider,
        "model": candidate.model,
        "credential_ref": candidate.credential,
    }


def _path(ordinal: int, target: Target, state: str, exclusion: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "provider": target.provider_id,
        "model": target.model,
        "credential_ref": target.credential_id,
        "cost_tier": target.cost_tier,
        "state": state,
        "exclusion": exclusion,
    }


def _identities(config: CerberusConfig, alias_name: str, alias: Alias) -> list[dict[str, Any]]:
    """Every configured identity's standing against this alias.

    The operator console wants the alias's whole authorization matrix, not one
    caller's view, so this answers for all of them — with the same predicate the
    inference path uses to admit or refuse a request.
    """

    rows: list[dict[str, Any]] = []
    for name, identity in config.identities.items():
        denial = authorization_error(IdentityContext(name=name, identity=identity), alias_name, alias)
        rows.append({"name": name, "authorized": denial is None, "denial_reason": denial})
    return rows


def _paths(
    config: CerberusConfig,
    alias_name: str,
    alias: Alias,
    *,
    control_plane: Any | None,
    store: Any,
    now: float | None,
) -> list[dict[str, Any]]:
    targets = ordered_targets(config, alias_name)
    eligible, cost_exclusions = cost_eligible(alias, targets)
    # cost_eligible appends exclusions in target order, so the k-th refused
    # target is the k-th exclusion; identity comparison keeps duplicate
    # provider/model pairs distinct where a name-based join could not
    eligible_ids = {id(target) for target in eligible}
    refusals = iter(cost_exclusions)

    paths: list[dict[str, Any]] = []
    attemptable_seen = False
    for ordinal, target in enumerate(targets):
        if id(target) not in eligible_ids:
            # policy refused it first; the router never reaches the later gates,
            # so neither do we — a prohibited paid path reports the prohibition
            # even when it is also cooled down
            reason = next(refusals)["reason"]
            paths.append(_path(ordinal, target, STATE_EXCLUDED, {"reason": reason, "scope": None, "retry_at": None}))
            continue
        blocked = runtime_exclusion(target, control_plane=control_plane, store=store, now=now)
        if blocked is not None:
            paths.append(
                _path(
                    ordinal,
                    target,
                    STATE_EXCLUDED,
                    {"reason": blocked.reason, "scope": blocked.scope, "retry_at": blocked.retry_at},
                )
            )
            continue
        paths.append(_path(ordinal, target, STATE_STANDBY if attemptable_seen else STATE_ELIGIBLE, None))
        attemptable_seen = True
    return paths


def _fusion(
    config: CerberusConfig,
    alias: Alias,
    *,
    backend_names: Collection[str],
    control_plane: Any | None,
) -> dict[str, Any]:
    assert alias.fusion is not None
    policy = alias.fusion
    return {
        "backend": policy.backend,
        "panel": [_member(candidate, order) for order, candidate in enumerate(alias.candidates)],
        "analyst": _participant(policy.judge),
        # resolved server-side: the config leaves `outer` null when it defaults
        # to the judge, and that default is the router's to apply, not a
        # reader's to infer
        "outer": _participant(policy.outer_model),
        "max_panel_members": policy.max_panel_members,
        "allow_paid_panel": policy.allow_paid_panel,
        "timeout_seconds": policy.timeout_seconds,
        "readiness": fusion_readiness(
            config, alias, backend_names=backend_names, control_plane=control_plane
        ),
    }


def route_projection(
    document: ConfigDocument,
    *,
    control_plane: Any | None,
    store: Any,
    backend_names: Collection[str] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """The active revision's routes, with every state decided server-side."""

    config = document.config
    aliases: list[dict[str, Any]] = []
    for alias_name, alias in config.aliases.items():
        projected: dict[str, Any] = {
            "alias": alias_name,
            "mode": alias.mode,
            "identities": _identities(config, alias_name, alias),
        }
        if alias.mode == "fusion":
            projected["fusion"] = _fusion(
                config, alias, backend_names=backend_names, control_plane=control_plane
            )
        else:
            projected["allow_paid_fallback"] = alias.allow_paid_fallback
            projected["paths"] = _paths(
                config, alias_name, alias, control_plane=control_plane, store=store, now=now
            )
        aliases.append(projected)

    return {"revision": document.version, "checksum": document.checksum, "aliases": aliases}

"""The pool a model-selection strategy may choose from, after Cerberus's filters.

Some aliases do not walk an ordered failover list. They hand a set of models to
a strategy that picks one — today OpenRouter's hosted Jev Router. Cerberus stays
the policy engine for those aliases: the strategy sees only what survives every
filter Cerberus applies, in this order:

    registry policy   ordered_targets(): configured, validated candidates only
    cost              a paid model leaves the pool unless the alias allows paid
    availability      runtime_exclusion(): health, then cooldown, then credential

The order matters for the same reason it does in the failover loop: a member
that fails more than one gate reports the first, so a prohibited paid model
reports the prohibition even while it is also cooled down. The availability
gates are the loop's own, read from ``router/availability.py``, so a pool and a
failover path can never disagree about whether a provider is reachable.

Nothing here selects a model, opens a connection or applies a cooldown. Dispatch
and ``/admin/routes`` both call it, so the console reports the pool a request
would actually send.
"""

from __future__ import annotations

from dataclasses import dataclass

from cerberus.registry.schema import CerberusConfig
from cerberus.router.availability import CooldownSource, HealthSource, RuntimeExclusion, runtime_exclusion
from cerberus.router.engine import Target, ordered_targets


@dataclass(frozen=True, slots=True)
class PoolMember:
    target: Target
    # None when the strategy may choose this member right now
    exclusion: RuntimeExclusion | None


def resolve_pool(
    config: CerberusConfig,
    alias_name: str,
    *,
    allow_paid: bool,
    control_plane: HealthSource | None,
    store: CooldownSource,
    now: float | None = None,
) -> list[PoolMember]:
    """Every configured candidate in config order, each with its first refusal."""

    members: list[PoolMember] = []
    for target in ordered_targets(config, alias_name):
        if target.cost_tier == "paid" and not allow_paid:
            # schema validation already forbids this; defense in depth
            members.append(PoolMember(target, RuntimeExclusion("paid_pool_prohibited")))
            continue
        members.append(
            PoolMember(target, runtime_exclusion(target, control_plane=control_plane, store=store, now=now))
        )
    return members

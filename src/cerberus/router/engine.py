"""Deterministic target resolution and cost-tier gating.

Policy dispatch is deterministic: same config version + same alias -> same
ordered target list. Runtime failover over that order is state-dependent and
belongs to the dispatch loop, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from cerberus.registry.schema import Alias, CerberusConfig, CostTier


@dataclass(frozen=True, slots=True)
class Target:
    provider_id: str
    credential_id: str
    model: str
    cost_tier: CostTier
    base_url: str
    api_key_env: str
    quota_cooldown_seconds: int
    transport_cooldown_seconds: int
    quota_scope: str
    reasoning_effort: str | None = None
    reasoning_effort_override: bool = False
    # Not hashed anywhere (Target is never a dict key or set member), so a
    # mutable mapping on a frozen dataclass is safe here.
    chat_template_kwargs: Mapping[str, Any] | None = None
    chat_template_kwargs_override: bool = False

    def replace_cost_tier(self, cost_tier: CostTier) -> "Target":
        return replace(self, cost_tier=cost_tier)

    def describe(self) -> dict:
        described: dict[str, Any] = {
            "provider": self.provider_id,
            "credential": self.credential_id,
            "model": self.model,
        }
        # Surfaced in telemetry only when configured, so later cost/quality
        # evidence is attributable to the reasoning budget actually used.
        if self.reasoning_effort is not None:
            described["reasoning_effort"] = self.reasoning_effort
        # Same rationale: a thinking-disabled run and a thinking-enabled run must
        # be distinguishable after the fact, or their latency and quality numbers
        # cannot be compared.
        if self.chat_template_kwargs is not None:
            described["chat_template_kwargs"] = dict(self.chat_template_kwargs)
        return described


def ordered_targets(config: CerberusConfig, alias_name: str) -> list[Target]:
    """The ordered policy decision for an alias — pure function of the config."""
    alias = config.aliases[alias_name]
    targets: list[Target] = []
    for candidate in alias.candidates:
        provider = config.providers[candidate.provider]
        model = provider.models[candidate.model]
        targets.append(
            Target(
                provider_id=candidate.provider,
                credential_id=candidate.credential,
                model=candidate.model,
                cost_tier=model.cost_tier,
                base_url=str(provider.base_url).rstrip("/"),
                api_key_env=provider.credentials[candidate.credential].api_key_env,
                quota_cooldown_seconds=provider.quota_cooldown_seconds,
                transport_cooldown_seconds=provider.transport_cooldown_seconds,
                quota_scope=provider.quota_scope,
                reasoning_effort=candidate.reasoning_effort,
                reasoning_effort_override=candidate.reasoning_effort_override,
                chat_template_kwargs=candidate.chat_template_kwargs,
                chat_template_kwargs_override=candidate.chat_template_kwargs_override,
            )
        )
    return targets


def cost_eligible(alias: Alias, targets: list[Target]) -> tuple[list[Target], list[dict]]:
    """Apply the cost-tier policy gate. Returns (eligible targets, exclusions with reasons)."""
    eligible: list[Target] = []
    exclusions: list[dict] = []
    has_free = any(target.cost_tier == "free" for target in targets)
    for target in targets:
        if target.cost_tier == "paid":
            if alias.mode == "free":
                # schema validation forbids this; runtime guard is defense in depth
                exclusions.append({**target.describe(), "reason": "cost_tier_guard"})
                continue
            if has_free and not alias.allow_paid_fallback:
                exclusions.append({**target.describe(), "reason": "paid_fallback_prohibited"})
                continue
        eligible.append(target)
    return eligible, exclusions

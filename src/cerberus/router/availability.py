"""Runtime gates shared by the failover loop and the admin route projection.

Cost-tier policy is decided once, up front, by ``engine.cost_eligible``. What
remains is state that changes between requests: provider health, scoped
cooldowns and credential presence. The dispatch loop walks those three in a
fixed order and skips a target at the first one that refuses it.

``/admin/routes`` has to answer the same question — *would this target be
skipped right now, and why* — for an operator rather than for a request. That
answer must be the router's, not a second opinion: an admin endpoint that
restates the order is free to drift from the loop it claims to describe. So the
order lives here once and both callers read it from the same function.

Nothing here opens an upstream connection, applies a cooldown, or decides
policy. It reads facts that already exist. The one write anywhere beneath it is
pre-existing and inert: ``active_for`` drops cooldown rows whose ``retry_at``
has already passed, which excludes nothing either way and would be dropped by
the next routed request regardless.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cerberus.router.engine import Target
from cerberus.state.cooldowns import Cooldown


class HealthSource(Protocol):
    def provider_available(self, provider: str, *, now: float | None = None) -> bool: ...


class CooldownSource(Protocol):
    def active_for(
        self, provider: str, credential: str, model: str, *, now: float | None = None
    ) -> Cooldown | None: ...


@dataclass(frozen=True, slots=True)
class RuntimeExclusion:
    """Why the router would skip a target before making an upstream request."""

    reason: str
    scope: str | None = None
    retry_at: float | None = None

    def telemetry(self) -> dict[str, Any]:
        """Exactly the exclusion fields a routing event has always carried.

        ``retry_at`` is deliberately absent: the event stream never carried it,
        and this is not the change that adds it.
        """

        record: dict[str, Any] = {"reason": self.reason}
        if self.scope is not None:
            record["scope"] = self.scope
        return record


def runtime_exclusion(
    target: Target,
    *,
    control_plane: HealthSource | None,
    store: CooldownSource,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> RuntimeExclusion | None:
    """The first runtime gate that would skip ``target``, or None if attemptable.

    The order is the dispatch loop's and must stay that way — health, then the
    most-specific active cooldown, then credential presence — because a target
    that fails more than one gate has to report the reason the router would
    actually reach first.
    """

    env = os.environ if environ is None else environ
    if control_plane is not None and not control_plane.provider_available(target.provider_id):
        return RuntimeExclusion("provider_down")
    cooldown = store.active_for(target.provider_id, target.credential_id, target.model, now=now)
    if cooldown is not None:
        return RuntimeExclusion(
            f"cooldown_{cooldown.reason}", scope=cooldown.scope, retry_at=cooldown.retry_at
        )
    if not env.get(target.api_key_env, "").strip():
        return RuntimeExclusion("missing_credentials")
    return None

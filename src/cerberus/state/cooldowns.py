"""Scoped cooldown state — (provider, credential, model) keys, wall-clock retry_at.

In-memory implementation for Session 2; Session 3 adds the persistent backend
behind the same interface. Wall-clock time (not monotonic) is deliberate:
persisted `retry_at` must survive restarts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

CooldownScope = Literal["model", "credential", "provider"]


@dataclass(frozen=True, slots=True)
class Cooldown:
    scope: CooldownScope
    provider: str
    credential: str | None
    model: str | None
    reason: str
    retry_at: float  # unix epoch seconds


class InMemoryCooldownStore:
    def __init__(self) -> None:
        self._cooldowns: dict[tuple[str, str | None, str | None], Cooldown] = {}

    @staticmethod
    def _key(scope: CooldownScope, provider: str, credential: str | None, model: str | None):
        if scope == "provider":
            return (provider, None, None)
        if scope == "credential":
            return (provider, credential, None)
        return (provider, credential, model)

    def apply(
        self,
        *,
        scope: CooldownScope,
        provider: str,
        credential: str | None,
        model: str | None,
        reason: str,
        duration_seconds: float,
        now: float | None = None,
    ) -> Cooldown:
        started = time.time() if now is None else now
        cooldown = Cooldown(
            scope=scope,
            provider=provider,
            credential=credential if scope != "provider" else None,
            model=model if scope == "model" else None,
            reason=reason,
            retry_at=started + max(1.0, duration_seconds),
        )
        key = self._key(scope, provider, credential, model)
        existing = self._cooldowns.get(key)
        # a later retry_at always wins; never shorten an active cooldown
        if existing is None or cooldown.retry_at > existing.retry_at:
            self._cooldowns[key] = cooldown
        return self._cooldowns[key]

    def active_for(
        self, provider: str, credential: str, model: str, *, now: float | None = None
    ) -> Cooldown | None:
        """Most-specific active cooldown covering this target, or None."""
        current = time.time() if now is None else now
        for key in (
            (provider, credential, model),
            (provider, credential, None),
            (provider, None, None),
        ):
            cooldown = self._cooldowns.get(key)
            if cooldown is not None:
                if cooldown.retry_at > current:
                    return cooldown
                del self._cooldowns[key]
        return None

    def snapshot(self, *, now: float | None = None) -> list[dict]:
        current = time.time() if now is None else now
        active = []
        for cooldown in list(self._cooldowns.values()):
            remaining = cooldown.retry_at - current
            if remaining <= 0:
                continue
            active.append(
                {
                    "scope": cooldown.scope,
                    "provider": cooldown.provider,
                    "credential": cooldown.credential,
                    "model": cooldown.model,
                    "reason": cooldown.reason,
                    "seconds_remaining": round(remaining, 2),
                }
            )
        return sorted(active, key=lambda entry: (entry["provider"], entry["scope"]))

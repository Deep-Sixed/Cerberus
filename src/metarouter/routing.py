"""Policy selection and process-local provider cooldowns."""

from dataclasses import dataclass
import random
import time

from .config import MAX_ROUTING_LABEL_LENGTH, PoolConfig, RouterConfig, RoutingRule

DEFAULT_REQUEST_TYPE = "default"


class RoutingError(RuntimeError):
    """No eligible configured provider can serve a request."""


@dataclass(frozen=True, slots=True)
class Selection:
    provider_id: str
    model: str
    pool: str
    used_fallback: bool


class Router:
    """Own policy state for one process; credentials remain outside the router."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self._cooldowns: dict[str, float] = {}
        self._round_robin_positions: dict[str, int] = {}
        self._configured_request_types = frozenset(
            request_type
            for rule in config.routing_rules
            if isinstance((request_type := rule.match.get("request_type")), str)
        )

    def canonical_request_type(self, candidate: object) -> str:
        """Return a trusted routing label without persisting caller-controlled text."""

        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate) > MAX_ROUTING_LABEL_LENGTH
            or candidate not in self._configured_request_types
        ):
            return DEFAULT_REQUEST_TYPE
        return candidate

    def _rule_for(self, request_type: str) -> RoutingRule:
        for rule in self.config.routing_rules:
            if rule.match.get("request_type") == request_type:
                return rule
        for rule in self.config.routing_rules:
            if rule.match.get("default") is True:
                return rule
        raise RoutingError(f"No routing rule for request_type={request_type!r}")

    def _live_provider_ids(self, pool: PoolConfig, excluded: set[str]) -> list[str]:
        now = time.monotonic()
        return [
            provider_id
            for provider_id in pool.providers
            if provider_id not in excluded and self._cooldowns.get(provider_id, 0) <= now
        ]

    def _select_from_pool(self, pool_name: str, excluded: set[str]) -> str | None:
        pool = self.config.pools[pool_name]
        live = self._live_provider_ids(pool, excluded)
        if not live:
            return None
        if pool.strategy == "first":
            return live[0]
        if pool.strategy == "random":
            return random.choice(live)
        start = self._round_robin_positions.get(pool_name, 0)
        for offset in range(len(pool.providers)):
            index = (start + offset) % len(pool.providers)
            provider_id = pool.providers[index]
            if provider_id in live:
                self._round_robin_positions[pool_name] = index + 1
                return provider_id
        return None

    def select(self, request_type: str, *, excluded: set[str] | None = None) -> Selection:
        excluded = excluded or set()
        rule = self._rule_for(request_type)
        provider_id = self._select_from_pool(rule.pool, excluded)
        used_fallback = False
        pool_name = rule.pool
        if provider_id is None and rule.fallback_pool and rule.fallback_pool != rule.pool:
            provider_id = self._select_from_pool(rule.fallback_pool, excluded)
            pool_name = rule.fallback_pool
            used_fallback = provider_id is not None
        if provider_id is None:
            raise RoutingError(f"No live providers in pools: {rule.pool}, {rule.fallback_pool or '-'}")
        provider = self.config.providers[provider_id]
        return Selection(provider_id=provider_id, model=provider.model, pool=pool_name, used_fallback=used_fallback)

    def cool(self, provider_id: str, seconds: float | None = None) -> None:
        configured = self.config.providers[provider_id].cooldown_seconds
        duration = configured if seconds is None else max(1.0, min(seconds, configured))
        self._cooldowns[provider_id] = time.monotonic() + duration

    def cooldown_state(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            provider_id: round(expires_at - now, 2)
            for provider_id, expires_at in self._cooldowns.items()
            if expires_at > now
        }

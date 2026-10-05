"""jev-router mode: OpenRouter's hosted Jev Router as one strategy under Cerberus policy.

Cerberus decides which models may serve the alias and filters them by cost,
health, cooldown and credential before every request; Jev picks one model from
what survives. Cerberus verifies the router's decision against the pool it sent
and withholds any response that left it. Jev chooses; Cerberus governs.
"""

from cerberus.jev_router.backend import (
    JEV_ROUTER_MODEL,
    JevRouterBackend,
    JevRouterError,
    JevRouterRequest,
    JevRouterResult,
    OpenRouterJevRouterBackend,
    RouterDecision,
)
from cerberus.jev_router.dispatch import jev_router_dispatch, jev_router_readiness

__all__ = [
    "JEV_ROUTER_MODEL",
    "JevRouterBackend",
    "JevRouterError",
    "JevRouterRequest",
    "JevRouterResult",
    "OpenRouterJevRouterBackend",
    "RouterDecision",
    "jev_router_dispatch",
    "jev_router_readiness",
]

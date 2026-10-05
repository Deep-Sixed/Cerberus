"""jev mode: Jev as Cerberus's model-selection intelligence, Cerberus as executor.

Cerberus filters a pool that may span every provider it knows — local hosts,
Cloudflare, OpenRouter — and asks Jev, through OpenRouter's Decisions API, one
question: which surviving model is the cheapest one strong enough for this
request. Cerberus then runs that model through its own failover loop. Jev
decides; Cerberus governs and executes.
"""

from cerberus.jev.backend import (
    Decision,
    DecisionBackend,
    DecisionError,
    DecisionRequest,
    OpenRouterJevDecider,
)
from cerberus.jev.dispatch import decider_readiness, jev_dispatch

__all__ = [
    "Decision",
    "DecisionBackend",
    "DecisionError",
    "DecisionRequest",
    "OpenRouterJevDecider",
    "decider_readiness",
    "jev_dispatch",
]

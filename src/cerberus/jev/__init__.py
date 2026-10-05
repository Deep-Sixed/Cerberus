"""jev mode: Jev as Cerberus's model-selection intelligence, Cerberus as executor.

Cerberus filters a pool that may span every provider it knows — local hosts,
Cloudflare, OpenRouter — and asks Jev, through OpenRouter's Decisions API,
which surviving model is the cheapest one strong enough for this request and,
by policy, how much reasoning effort it needs. Cerberus then runs that model
through its own failover loop. Jev decides; Cerberus governs and executes.
"""

from cerberus.jev.backend import (
    Answer,
    Decision,
    DecisionBackend,
    DecisionError,
    DecisionRequest,
    OpenRouterJevDecider,
    Question,
)
from cerberus.jev.dispatch import decider_readiness, jev_dispatch

__all__ = [
    "Answer",
    "Decision",
    "DecisionBackend",
    "DecisionError",
    "DecisionRequest",
    "OpenRouterJevDecider",
    "Question",
    "decider_readiness",
    "jev_dispatch",
]

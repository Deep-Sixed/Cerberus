"""Fusion mode: Cerberus policy in front of a managed deliberation backend.

Cerberus decides whether a request is a fusion request, which panel and analyst
run, and under which credential and deadline. A ``FusionBackend`` performs the
deliberation; ``OpenRouterFusionBackend`` is the initial managed implementation.
No panel/judge execution lives in this package.
"""

from cerberus.fusion.backend import (
    FusionBackend,
    FusionError,
    FusionRequest,
    FusionResult,
    OpenRouterFusionBackend,
)
from cerberus.fusion.dispatch import fusion_aliases, fusion_dispatch, fusion_status

__all__ = [
    "FusionBackend",
    "FusionError",
    "FusionRequest",
    "FusionResult",
    "OpenRouterFusionBackend",
    "fusion_aliases",
    "fusion_dispatch",
    "fusion_status",
]

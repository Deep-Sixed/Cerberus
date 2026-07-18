"""Fusion-mode policy and worker client.

Panel/judge EXECUTION lives in cerberus-fusion-worker (the fusion_backend fork),
never here (S10-S11). This package holds the config generator (Cerberus fusion
alias → worker runtime config) and the worker client Cerberus fans out through.
"""

from cerberus.fusion.client import FusionWorkerClient, fusion_dispatch
from cerberus.fusion.generator import (
    DEFAULT_WORKER_TOKEN_ENV,
    fusion_aliases,
    generate_worker_config,
    generate_worker_config_yaml,
)

__all__ = [
    "DEFAULT_WORKER_TOKEN_ENV",
    "FusionWorkerClient",
    "fusion_aliases",
    "fusion_dispatch",
    "generate_worker_config",
    "generate_worker_config_yaml",
]

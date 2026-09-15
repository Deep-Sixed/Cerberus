"""ONE selection engine: ordered candidates, cost-tier gate, cooldown exclusion — free/dispatch/fusion are alias modes, not routers."""

from cerberus.router.dispatch import dispatch
from cerberus.router.engine import Target, cost_eligible, ordered_targets

__all__ = ["Target", "cost_eligible", "dispatch", "ordered_targets"]

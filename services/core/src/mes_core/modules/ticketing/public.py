"""Public interface of the ticketing module."""

from mes_core.modules.ticketing.demo import SUPPORT_TEAM, load_demo_tickets
from mes_core.modules.ticketing.service import apply_external, apply_sync_result, check_sla, raise_automatic

__all__ = ["SUPPORT_TEAM", "apply_external", "apply_sync_result", "check_sla", "load_demo_tickets", "raise_automatic"]

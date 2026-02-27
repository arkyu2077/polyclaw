"""Live trading module — thin facade re-exporting from split modules.

All logic has moved to:
  - order_executor.py  — CLOB order placement/cancellation
  - position_tracker.py — Position state management
  - exit_manager.py    — TP/SL/timeout/stale order logic
  - redemption.py      — On-chain CTF token redemption
"""

# Re-export all public functions for backward compatibility
from order_executor import get_balance, get_open_orders, place_limit_order, release_funds_for_signal
from position_tracker import (
    get_live_positions,
    open_live_position,
    close_live_position,
    check_pending_orders,
)
from exit_manager import check_live_exits, cleanup_stale_orders
from redemption import auto_redeem_resolved

__all__ = [
    "get_balance",
    "get_open_orders",
    "place_limit_order",
    "release_funds_for_signal",
    "get_live_positions",
    "open_live_position",
    "close_live_position",
    "check_pending_orders",
    "check_live_exits",
    "cleanup_stale_orders",
    "auto_redeem_resolved",
]

"""Exit manager — TP/SL/timeout/stale order logic for live positions."""

import time
from datetime import datetime, timezone

from rich.console import Console

from .config import get_config
from .db import get_positions, add_notification
from .order_executor import _get_client
from .position_tracker import close_live_position, check_pending_orders

console = Console()


def check_live_exits() -> int:
    """Check live positions for exit conditions. Returns count of closed positions."""
    # First check pending orders for fills
    check_pending_orders()

    positions = get_positions(mode="live", status="open")
    if not positions:
        return 0

    closed = 0
    for pos in positions:
        market_id = pos["market_id"]

        # Get current price from CLOB
        try:
            client = _get_client()
            market = client.get_market(market_id)
            tokens = market.get("tokens", [])

            current_price = None
            for t in tokens:
                if t.get("token_id") == pos["token_id"]:
                    current_price = float(t.get("price", 0))
                    break

            if current_price is None:
                continue

        except Exception:
            continue

        # Check take profit
        if pos.get("target_price") and current_price >= pos["target_price"]:
            result = close_live_position(pos, "TAKE_PROFIT")
            if result:
                closed += 1
            continue

        # Check stop loss
        if pos.get("stop_loss") and current_price <= pos["stop_loss"]:
            result = close_live_position(pos, "STOP_LOSS")
            if result:
                closed += 1
            continue

        # Check timeout
        if pos.get("entry_time"):
            cfg = get_config()
            entry_dt = datetime.fromisoformat(pos["entry_time"])
            hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            if hours > cfg.live_timeout_hours:
                result = close_live_position(pos, "TIMEOUT")
                if result:
                    closed += 1
                continue

    return closed


def cleanup_stale_orders() -> int:
    """Cancel stale orders: >N hours old, market expiring <1h, or price drifted >threshold.
    Returns count of cancelled orders."""
    cfg = get_config()
    client = _get_client()
    orders = client.get_orders()
    if not orders:
        return 0

    now = datetime.now(timezone.utc)
    to_cancel = []

    for o in orders:
        order_id = o.get("id", "")
        created = int(o.get("created_at", 0))
        price = float(o.get("price", 0))
        market_id = o.get("market", "")
        outcome = o.get("outcome", "")
        size_matched = float(o.get("size_matched", 0))
        original_size = float(o.get("original_size", 0))

        # Skip if partially filled (>50% filled, let it ride)
        if original_size > 0 and size_matched / original_size > 0.5:
            continue

        reason = None

        # 1. Timeout: >N hours since creation
        if created > 0:
            age_hours = (now.timestamp() - created) / 3600
            if age_hours > cfg.stale_order_hours:
                reason = f"超时({age_hours:.0f}h)"

        # 2. Market expiring soon or price drifted
        if not reason and market_id:
            try:
                mdata = client.get_market(market_id)
                tokens = mdata.get("tokens", [])

                end_date = mdata.get("end_date_iso", "") or ""
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_left = (end_dt - now).total_seconds() / 3600
                        if hours_left < 1:
                            reason = f"即将到期({hours_left:.1f}h)"
                    except Exception:
                        pass

                if not reason:
                    for t in tokens:
                        if t.get("outcome") == outcome:
                            current = float(t.get("price", 0))
                            if current > 0 and price > 0:
                                drift = abs(current - price) / price
                                if drift > cfg.price_drift_threshold:
                                    reason = f"价格偏离({drift:.0%})"
                            break
            except Exception:
                pass

        if reason:
            to_cancel.append((order_id, reason, o))

    cancelled = 0
    for order_id, reason, o in to_cancel:
        try:
            client.cancel(order_id)
            cost = float(o.get("original_size", 0)) * float(o.get("price", 0))
            console.print(f"[yellow]  🗑️ 撤单: {o.get('outcome','')} @${float(o.get('price',0)):.2f} | ${cost:.1f} | {reason}[/yellow]")
            add_notification(
                f"🗑️ 自动撤单: {o.get('outcome','')} @${float(o.get('price',0)):.2f} | 原因: {reason} | 释放${cost:.1f}",
                "CANCEL",
            )
            cancelled += 1
            time.sleep(0.5)
        except Exception as e:
            console.print(f"[red]  ❌ 撤单失败 {order_id[:16]}: {e}[/red]")

    return cancelled

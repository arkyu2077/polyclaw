"""Decision Journal — structured audit trail for every trade lifecycle.

Each trade gets a single row in the decisions table that accumulates events
from signal → settlement. Designed for post-mortem review and future UI display.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from db import upsert_decision, get_decision, get_decisions


# ═══════════════════════════════════════════
# Public API — call these from live_trader.py
# ═══════════════════════════════════════════

def record_signal(
    trade_id: str,
    market_id: str,
    question: str,
    direction: str,
    source: str,
    trigger_news: str,
    ai_probability: float,
    market_price: float,
    edge: float,
    confidence: float,
    reasoning: str = "",
    extra: dict = None,
) -> dict:
    """Step 1: Signal discovered. Creates the decision record."""
    now = datetime.now(timezone.utc).isoformat()
    signal_data = {
        "time": now,
        "direction": direction,
        "source": source,
        "trigger_news": trigger_news[:500],
        "ai_probability": round(ai_probability, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "confidence": round(confidence, 4),
        "reasoning": reasoning[:500],
        **(extra or {}),
    }
    events = [
        {"time": now, "type": "signal_detected", "detail": f"{source}: {trigger_news[:100]}"}
    ]

    decision = {
        "trade_id": trade_id,
        "status": "signal",
        "market_id": market_id,
        "question": question[:80],
        "direction": direction,
        "signal_data": signal_data,
        "decision_data": None,
        "order_data": None,
        "fill_data": None,
        "settlement_data": None,
        "events": events,
    }
    upsert_decision(decision)
    return decision


def record_decision(
    trade_id: str,
    action: str,
    size_usd: float,
    price: float,
    shares: int,
    target_price: float = 0,
    stop_loss: float = 0,
    reason: str = "",
) -> Optional[dict]:
    """Step 2: Decision to open (or skip). Updates the decision record."""
    decision = get_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "decided"
    decision["decision_data"] = {
        "time": now,
        "action": action,
        "size_usd": round(size_usd, 2),
        "price": round(price, 4),
        "shares": shares,
        "target_price": round(target_price, 4),
        "stop_loss": round(stop_loss, 4),
        "reason": reason,
    }
    events = decision.get("events") or []
    if isinstance(events, str):
        events = json.loads(events)
    events.append({
        "time": now,
        "type": f"decision_{action}",
        "detail": f"${size_usd:.2f} @ {price:.4f} ({shares} shares)" if action == "open" else reason,
    })
    decision["events"] = events
    upsert_decision(decision)
    return decision


def record_order(
    trade_id: str,
    order_id: str,
    token_id: str,
    side: str,
    price: float,
    shares: int,
    cost: float,
    neg_risk: bool = False,
) -> Optional[dict]:
    """Step 3: Order placed on CLOB. Records order details."""
    decision = get_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "ordered"
    decision["order_data"] = {
        "time": now,
        "order_id": order_id,
        "token_id": token_id,
        "side": side,
        "price": round(price, 4),
        "shares": shares,
        "cost": round(cost, 2),
        "neg_risk": neg_risk,
    }
    events = decision.get("events") or []
    if isinstance(events, str):
        events = json.loads(events)
    events.append({
        "time": now,
        "type": "order_placed",
        "detail": f"{side} {shares}x @ {price:.4f} = ${cost:.2f} (order {order_id[:12]})",
    })
    decision["events"] = events
    upsert_decision(decision)
    return decision


def record_fill(
    trade_id: str,
    fill_price: float = 0,
    fill_shares: int = 0,
    fill_cost: float = 0,
    partial: bool = False,
) -> Optional[dict]:
    """Step 4: Order filled (fully or partially)."""
    decision = get_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "filled" if not partial else "partial_fill"
    decision["fill_data"] = {
        "time": now,
        "fill_price": round(fill_price, 4),
        "fill_shares": fill_shares,
        "fill_cost": round(fill_cost, 2),
        "partial": partial,
    }
    label = "partial_fill" if partial else "order_filled"
    events = decision.get("events") or []
    if isinstance(events, str):
        events = json.loads(events)
    events.append({
        "time": now,
        "type": label,
        "detail": f"{fill_shares}x @ {fill_price:.4f} = ${fill_cost:.2f}",
    })
    decision["events"] = events
    upsert_decision(decision)
    return decision


def record_settlement(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    fees: float = 0,
    duration_hours: float = 0,
    tx_hash: str = "",
) -> Optional[dict]:
    """Step 5: Position closed or redeemed — final settlement."""
    decision = get_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "settled"
    decision["settlement_data"] = {
        "time": now,
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 2),
        "fees": round(fees, 2),
        "net_pnl": round(pnl - fees, 2),
        "duration_hours": round(duration_hours, 2),
        "tx_hash": tx_hash,
    }
    icon = "✅" if pnl >= 0 else "❌"
    events = decision.get("events") or []
    if isinstance(events, str):
        events = json.loads(events)
    events.append({
        "time": now,
        "type": "settled",
        "detail": f"{icon} {exit_reason}: ${pnl:+.2f} (fees ${fees:.2f}, net ${pnl - fees:+.2f}) after {duration_hours:.1f}h",
    })
    decision["events"] = events
    upsert_decision(decision)
    return decision


def add_event(trade_id: str, event_type: str, detail: str) -> Optional[dict]:
    """Add a custom event to the timeline (e.g., price updates, rebalance checks)."""
    decision = get_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    events = decision.get("events") or []
    if isinstance(events, str):
        events = json.loads(events)
    events.append({"time": now, "type": event_type, "detail": detail})
    decision["events"] = events
    upsert_decision(decision)
    return decision


# ═══════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════

def get_recent_decisions(days: int = 7, status: str = None) -> list[dict]:
    """Get recent decisions for review. Optionally filter by status."""
    return get_decisions(status=status, limit=200)


def generate_review_report(days: int = 7) -> str:
    """Generate a markdown review report of recent decisions."""
    decisions = get_recent_decisions(days)
    if not decisions:
        return "No decisions in the last {} days.".format(days)

    settled = [d for d in decisions if d.get("status") == "settled"]
    active = [d for d in decisions if d.get("status") in ("ordered", "filled")]
    signals_only = [d for d in decisions if d.get("status") in ("signal", "decided")]

    total_pnl = sum((d.get("settlement_data") or {}).get("pnl", 0) for d in settled)
    wins = sum(1 for d in settled if (d.get("settlement_data") or {}).get("pnl", 0) > 0)
    losses = sum(1 for d in settled if (d.get("settlement_data") or {}).get("pnl", 0) <= 0)
    win_rate = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "N/A"

    lines = [
        f"# 决策复盘 — 最近 {days} 天",
        f"",
        f"## 总览",
        f"- 已结算: {len(settled)} 笔 (胜率 {win_rate}, {wins}W/{losses}L)",
        f"- 总 P&L: ${total_pnl:+.2f}",
        f"- 活跃: {len(active)} 笔",
        f"- 仅信号: {len(signals_only)} 笔",
        f"",
    ]

    if settled:
        lines.append("## 已结算交易")
        for d in settled:
            s = d.get("settlement_data") or {}
            sig = d.get("signal_data") or {}
            q = (d.get("question") or "?")[:60]
            icon = "🟢" if s.get("pnl", 0) >= 0 else "🔴"
            lines.append(
                f"- {icon} **{q}** | {sig.get('direction','')} | "
                f"${s.get('pnl',0):+.2f} | {s.get('exit_reason','')} | "
                f"{s.get('duration_hours',0):.1f}h"
            )
            news = sig.get("trigger_news", "")[:80]
            if news:
                lines.append(f"  触发: {news}")

    if active:
        lines.append("")
        lines.append("## 活跃持仓")
        for d in active:
            sig = d.get("signal_data") or {}
            o = d.get("order_data") or {}
            q = (d.get("question") or "?")[:60]
            lines.append(
                f"- ⏳ **{q}** | {sig.get('direction','')} | "
                f"${o.get('cost',0):.2f} @ {o.get('price',0):.4f}"
            )

    return "\n".join(lines)

"""Decision Journal — structured audit trail for every trade lifecycle.

Each trade gets a single JSON object that accumulates events from signal → settlement.
Designed for post-mortem review and future UI display.

File: decisions/YYYY-MM-DD/{trade_id}.json
Index: decisions/index.json (lightweight summary for fast loading)
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

DECISIONS_DIR = Path(__file__).parent / "decisions"
INDEX_FILE = DECISIONS_DIR / "index.json"


def _ensure_dir(dt: datetime = None):
    """Ensure decisions directory exists for the given date."""
    dt = dt or datetime.now(timezone.utc)
    day_dir = DECISIONS_DIR / dt.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def _load_decision(trade_id: str) -> Optional[dict]:
    """Load a decision by trade_id, searching recent date dirs."""
    # Check index first for fast lookup
    index = _load_index()
    if trade_id in index:
        path = Path(index[trade_id].get("path", ""))
        if path.exists():
            return json.loads(path.read_text())

    # Fallback: scan recent dirs
    if DECISIONS_DIR.exists():
        for day_dir in sorted(DECISIONS_DIR.iterdir(), reverse=True):
            if not day_dir.is_dir() or day_dir.name == "index.json":
                continue
            f = day_dir / f"{trade_id}.json"
            if f.exists():
                return json.loads(f.read_text())
    return None


def _save_decision(decision: dict):
    """Save a decision to its date directory and update index."""
    entry_time = decision.get("signal", {}).get("time") or decision.get("created_at", "")
    try:
        dt = datetime.fromisoformat(entry_time)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)

    day_dir = _ensure_dir(dt)
    trade_id = decision["trade_id"]
    path = day_dir / f"{trade_id}.json"
    path.write_text(json.dumps(decision, indent=2, ensure_ascii=False))

    # Update index
    _update_index(trade_id, decision, str(path))


def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _update_index(trade_id: str, decision: dict, path: str):
    index = _load_index()
    status = decision.get("status", "signal")
    pnl = (decision.get("settlement") or {}).get("pnl")
    question = decision.get("market", {}).get("question", "")[:80]

    index[trade_id] = {
        "path": path,
        "status": status,
        "question": question,
        "direction": decision.get("signal", {}).get("direction", ""),
        "cost": (decision.get("order") or {}).get("cost"),
        "pnl": pnl,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False))


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
    decision = {
        "trade_id": trade_id,
        "created_at": now,
        "status": "signal",
        "market": {
            "id": market_id,
            "question": question,
        },
        "signal": {
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
        },
        "decision": None,
        "order": None,
        "fill": None,
        "settlement": None,
        "events": [
            {"time": now, "type": "signal_detected", "detail": f"{source}: {trigger_news[:100]}"}
        ],
    }
    _save_decision(decision)
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
    decision = _load_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "decided"
    decision["decision"] = {
        "time": now,
        "action": action,  # "open" or "skip"
        "size_usd": round(size_usd, 2),
        "price": round(price, 4),
        "shares": shares,
        "target_price": round(target_price, 4),
        "stop_loss": round(stop_loss, 4),
        "reason": reason,
    }
    decision["events"].append({
        "time": now,
        "type": f"decision_{action}",
        "detail": f"${size_usd:.2f} @ {price:.4f} ({shares} shares)" if action == "open" else reason,
    })
    _save_decision(decision)
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
    decision = _load_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "ordered"
    decision["order"] = {
        "time": now,
        "order_id": order_id,
        "token_id": token_id,
        "side": side,
        "price": round(price, 4),
        "shares": shares,
        "cost": round(cost, 2),
        "neg_risk": neg_risk,
    }
    decision["events"].append({
        "time": now,
        "type": "order_placed",
        "detail": f"{side} {shares}x @ {price:.4f} = ${cost:.2f} (order {order_id[:12]})",
    })
    _save_decision(decision)
    return decision


def record_fill(
    trade_id: str,
    fill_price: float = 0,
    fill_shares: int = 0,
    fill_cost: float = 0,
    partial: bool = False,
) -> Optional[dict]:
    """Step 4: Order filled (fully or partially)."""
    decision = _load_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "filled" if not partial else "partial_fill"
    decision["fill"] = {
        "time": now,
        "fill_price": round(fill_price, 4),
        "fill_shares": fill_shares,
        "fill_cost": round(fill_cost, 2),
        "partial": partial,
    }
    label = "partial_fill" if partial else "order_filled"
    decision["events"].append({
        "time": now,
        "type": label,
        "detail": f"{fill_shares}x @ {fill_price:.4f} = ${fill_cost:.2f}",
    })
    _save_decision(decision)
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
    decision = _load_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["status"] = "settled"
    decision["settlement"] = {
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
    decision["events"].append({
        "time": now,
        "type": "settled",
        "detail": f"{icon} {exit_reason}: ${pnl:+.2f} (fees ${fees:.2f}, net ${pnl - fees:+.2f}) after {duration_hours:.1f}h",
    })
    _save_decision(decision)
    return decision


def add_event(trade_id: str, event_type: str, detail: str) -> Optional[dict]:
    """Add a custom event to the timeline (e.g., price updates, rebalance checks)."""
    decision = _load_decision(trade_id)
    if not decision:
        return None

    now = datetime.now(timezone.utc).isoformat()
    decision["events"].append({"time": now, "type": event_type, "detail": detail})
    _save_decision(decision)
    return decision


# ═══════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════

def get_recent_decisions(days: int = 7, status: str = None) -> list[dict]:
    """Get recent decisions for review. Optionally filter by status."""
    index = _load_index()
    results = []
    cutoff = datetime.now(timezone.utc).isoformat()[:10]  # not actually used for filtering here

    for trade_id, summary in index.items():
        if status and summary.get("status") != status:
            continue
        decision = _load_decision(trade_id)
        if decision:
            results.append(decision)

    # Sort by created_at descending
    results.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return results


def generate_review_report(days: int = 7) -> str:
    """Generate a markdown review report of recent decisions."""
    decisions = get_recent_decisions(days)
    if not decisions:
        return "No decisions in the last {} days.".format(days)

    settled = [d for d in decisions if d["status"] == "settled"]
    active = [d for d in decisions if d["status"] in ("ordered", "filled")]
    signals_only = [d for d in decisions if d["status"] in ("signal", "decided")]

    total_pnl = sum(d.get("settlement", {}).get("pnl", 0) for d in settled)
    wins = sum(1 for d in settled if d.get("settlement", {}).get("pnl", 0) > 0)
    losses = sum(1 for d in settled if d.get("settlement", {}).get("pnl", 0) <= 0)
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
            s = d.get("settlement", {})
            sig = d.get("signal", {})
            q = d.get("market", {}).get("question", "?")[:60]
            icon = "🟢" if s.get("pnl", 0) >= 0 else "🔴"
            lines.append(
                f"- {icon} **{q}** | {sig.get('direction','')} | "
                f"${s.get('pnl',0):+.2f} | {s.get('exit_reason','')} | "
                f"{s.get('duration_hours',0):.1f}h"
            )
            # Show trigger news
            news = sig.get("trigger_news", "")[:80]
            if news:
                lines.append(f"  触发: {news}")

    if active:
        lines.append("")
        lines.append("## 活跃持仓")
        for d in active:
            sig = d.get("signal", {})
            o = d.get("order", {})
            q = d.get("market", {}).get("question", "?")[:60]
            lines.append(
                f"- ⏳ **{q}** | {sig.get('direction','')} | "
                f"${o.get('cost',0):.2f} @ {o.get('price',0):.4f}"
            )

    return "\n".join(lines)

"""Position tracker — position state management for live trading."""

from datetime import datetime, timezone

from rich.console import Console

from .config import get_config
from .db import get_positions, upsert_position, insert_trade, add_notification, delete_positions_by_status
from .order_executor import _get_client, place_limit_order

console = Console()


def _notify_trade(position: dict, action: str):
    """Write trade notification to db for Discord alert pickup."""
    try:
        direction = position.get("direction", "?")
        question = position.get("question", "?")[:50]
        price = position.get("entry_price", 0)
        cost = position.get("cost", 0)

        if action == "OPEN":
            msg = f"💰 实盘下单: {direction} {question} @${price:.3f} | ${cost:.2f}"
            if position.get("trigger_news"):
                msg += f"\n📰 触发: {position['trigger_news'][:80]}"
            msg += f"\n🎯 止盈@${position.get('target_price', 0):.3f} | 止损@${position.get('stop_loss', 0):.3f}"
        elif action == "CLOSE":
            pnl = position.get("pnl", 0)
            reason = position.get("exit_reason", "?")
            icon = "🟢" if pnl >= 0 else "🔴"
            msg = f"{icon} 实盘平仓 [{reason}]: {question} | PnL: ${pnl:+.2f}"
        else:
            msg = f"📋 {action}: {question}"

        add_notification(msg, action)
    except Exception as e:
        console.print(f"[yellow]  ⚠ Notify error: {e}[/yellow]")


def get_live_positions() -> list[dict]:
    """Load live positions from db."""
    return get_positions(mode="live")


def open_live_position(
    market_id: str,
    token_id: str,
    question: str,
    direction: str,
    price: float,
    size_usd: float,
    trigger_news: str = "",
    target_price: float = 0,
    stop_loss: float = 0,
    neg_risk: bool = False,
    # Decision journal fields
    ai_probability: float = 0,
    edge: float = 0,
    confidence: float = 0,
    source: str = "scanner",
    reasoning: str = "",
) -> dict | None:
    """Open a live position: place order and track it."""
    from .decision_journal import (
        record_signal, record_decision, record_order, record_fill, add_event,
    )

    trade_id = f"T{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{market_id[-6:]}"

    # Journal Step 1: Signal
    try:
        record_signal(
            trade_id=trade_id, market_id=market_id, question=question,
            direction=direction, source=source, trigger_news=trigger_news,
            ai_probability=ai_probability, market_price=price, edge=edge,
            confidence=confidence, reasoning=reasoning,
        )
    except Exception as e:
        console.print(f"[dim]  Journal signal error: {e}[/dim]")

    shares = int(size_usd / price) if price > 0 else 0
    cfg = get_config()
    if shares < cfg.min_shares:
        console.print(f"[yellow]  ⚠ Too few shares ({shares}), skipping[/yellow]")
        try:
            record_decision(trade_id, "skip", size_usd, price, shares, reason=f"Too few shares ({shares})")
        except Exception:
            pass
        return None

    # Journal Step 2: Decision
    try:
        record_decision(trade_id, "open", size_usd, price, shares, target_price, stop_loss)
    except Exception as e:
        console.print(f"[dim]  Journal decision error: {e}[/dim]")

    result = place_limit_order(token_id, "BUY", price, shares, neg_risk)
    if not result:
        try:
            add_event(trade_id, "order_failed", "CLOB order placement failed")
        except Exception:
            pass
        return None

    order_id = result.get("orderID", "")
    cost = round(shares * price, 2)

    # Journal Step 3: Order placed
    try:
        record_order(trade_id, order_id, token_id, "BUY", price, shares, cost, neg_risk)
    except Exception as e:
        console.print(f"[dim]  Journal order error: {e}[/dim]")

    # Check immediate fill status
    initial_status = "pending"
    filled_shares = 0
    try:
        client = _get_client()
        order_info = client.get_order(order_id)
        size_matched = float(order_info.get("size_matched", 0))
        original_size = float(order_info.get("original_size", shares))
        if size_matched >= original_size * 0.95:
            initial_status = "open"
            filled_shares = int(size_matched)
            try:
                record_fill(trade_id, fill_price=price, fill_shares=filled_shares, fill_cost=round(filled_shares * price, 2))
            except Exception:
                pass
            console.print(f"[green]  ✅ Order fully filled ({filled_shares} shares)[/green]")
        elif size_matched > 0:
            initial_status = "partial"
            filled_shares = int(size_matched)
            try:
                record_fill(trade_id, fill_price=price, fill_shares=filled_shares, fill_cost=round(filled_shares * price, 2), partial=True)
            except Exception:
                pass
            console.print(f"[yellow]  ⏳ Partially filled ({filled_shares}/{shares} shares)[/yellow]")
        else:
            console.print(f"[yellow]  ⏳ Order pending (0/{shares} filled)[/yellow]")
    except Exception as e:
        console.print(f"[dim]  Could not check fill status: {e}[/dim]")

    position = {
        "id": order_id[:12],
        "trade_id": trade_id,
        "mode": "live",
        "strategy": "",
        "market_id": market_id,
        "token_id": token_id,
        "question": question,
        "direction": direction,
        "entry_price": price,
        "shares": shares,
        "filled_shares": filled_shares,
        "cost": cost,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "confidence": confidence,
        "status": initial_status,
        "order_id": order_id,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "trigger_news": trigger_news[:200],
        "neg_risk": 1 if neg_risk else 0,
    }

    upsert_position(position)
    _notify_trade(position, "OPEN")

    return position


def close_live_position(position: dict, reason: str = "manual") -> dict | None:
    """Close a live position by selling shares."""
    from .decision_journal import record_settlement
    from py_clob_client.clob_types import OrderArgs, OrderType

    client = _get_client()

    token_id = position["token_id"]
    shares = position["shares"]

    # Get current best bid
    try:
        book = client.get_order_book(token_id)
        if book.get("bids"):
            best_bid = float(book["bids"][0]["price"])
        else:
            console.print(f"[red]  ❌ No bids to sell into[/red]")
            return None
    except Exception as e:
        console.print(f"[red]  ❌ Failed to get orderbook: {e}[/red]")
        return None

    try:
        order_args = OrderArgs(
            price=best_bid,
            size=shares,
            side="SELL",
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, OrderType.GTC)

        # Calculate PnL
        entry = position["entry_price"]
        if position["direction"] == "BUY_YES":
            pnl = round((best_bid - entry) * shares, 2)
        else:
            pnl = round((entry - best_bid) * shares, 2)

        # Update position in db
        position["status"] = "closed"
        position["exit_price"] = best_bid
        position["exit_time"] = datetime.now(timezone.utc).isoformat()
        position["exit_reason"] = reason
        position["pnl"] = pnl
        upsert_position(position)

        # Insert trade history
        try:
            entry_dt = datetime.fromisoformat(position.get("entry_time", ""))
            hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 0

        insert_trade({
            "position_id": position["id"],
            "mode": "live",
            "strategy": "",
            "market_id": position.get("market_id"),
            "question": position.get("question"),
            "direction": position.get("direction"),
            "entry_price": entry,
            "exit_price": best_bid,
            "shares": shares,
            "cost": position.get("cost"),
            "pnl": pnl,
            "fees": 0,
            "entry_time": position.get("entry_time"),
            "exit_time": position["exit_time"],
            "exit_reason": reason,
            "trigger_news": position.get("trigger_news"),
            "confidence": position.get("confidence"),
            "hold_hours": round(hours, 2),
        })

        icon = "🟢" if pnl >= 0 else "🔴"
        console.print(f"  {icon} CLOSED LIVE [{reason}]: ${pnl:+.2f} ({entry:.1%}→{best_bid:.1%})")

        _notify_trade(position, "CLOSE")

        # Journal Step 5: Settlement
        trade_id = position.get("trade_id", "")
        if trade_id:
            try:
                record_settlement(trade_id, best_bid, reason, pnl, duration_hours=hours)
            except Exception as e:
                console.print(f"[dim]  Journal settlement error: {e}[/dim]")

        return result
    except Exception as e:
        console.print(f"[red]  ❌ Sell failed: {e}[/red]")
        return None


def check_pending_orders() -> int:
    """Check pending/partial orders for fill updates. Returns count of newly filled."""
    from .decision_journal import record_fill, add_event

    positions = get_positions(mode="live")
    pending = [p for p in positions if p.get("status") in ("pending", "partial")]
    if not pending:
        return 0

    client = _get_client()
    updated = 0

    for pos in pending:
        order_id = pos.get("order_id", "")
        if not order_id:
            continue

        try:
            order_info = client.get_order(order_id)
            size_matched = float(order_info.get("size_matched", 0))
            original_size = float(order_info.get("original_size", pos["shares"]))
            order_status = order_info.get("status", "")

            if size_matched >= original_size * 0.95 or order_status == "MATCHED":
                pos["status"] = "open"
                pos["filled_shares"] = int(size_matched)
                pos["cost"] = round(size_matched * pos["entry_price"], 2)
                upsert_position(pos)
                console.print(f"[green]  ✅ Order filled: {pos['question'][:40]}... ({int(size_matched)} shares)[/green]")
                _notify_trade(pos, "FILL")
                trade_id = pos.get("trade_id", "")
                if trade_id:
                    try:
                        record_fill(trade_id, pos["entry_price"], int(size_matched), pos["cost"])
                    except Exception:
                        pass
                updated += 1

            elif size_matched > pos.get("filled_shares", 0):
                pos["status"] = "partial"
                pos["filled_shares"] = int(size_matched)
                upsert_position(pos)
                console.print(f"[yellow]  ⏳ Partial fill: {pos['question'][:40]}... ({int(size_matched)}/{int(original_size)})[/yellow]")

            elif order_status == "CANCELLED":
                pos["status"] = "cancelled"
                upsert_position(pos)
                console.print(f"[red]  ❌ Order cancelled: {pos['question'][:40]}...[/red]")
                trade_id = pos.get("trade_id", "")
                if trade_id:
                    try:
                        add_event(trade_id, "order_cancelled", "Order cancelled on CLOB")
                    except Exception:
                        pass
                updated += 1

            else:
                if pos.get("entry_time"):
                    entry_dt = datetime.fromisoformat(pos["entry_time"])
                    hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                    if hours > 2 and size_matched == 0:
                        try:
                            client.cancel(order_id)
                            pos["status"] = "cancelled"
                            upsert_position(pos)
                            console.print(f"[yellow]  ⏰ Cancelled stale order (2h unfilled): {pos['question'][:40]}...[/yellow]")
                            updated += 1
                        except Exception:
                            pass

        except Exception as e:
            console.print(f"[dim]  Could not check order {order_id[:12]}: {e}[/dim]")

    # Remove cancelled positions
    delete_positions_by_status(mode="live", status="cancelled")
    return updated

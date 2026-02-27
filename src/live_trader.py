"""Live trading module — executes real orders on Polymarket CLOB via py_clob_client."""

import json
import time
from pathlib import Path
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from rich.console import Console
from decision_journal import (
    record_signal, record_decision, record_order, record_fill, record_settlement, add_event,
)
from .config import get_config

console = Console()

TRADE_NOTIFY_FILE = Path(__file__).parent / "TRADE_NOTIFY.json"

# Safety limits
MAX_ORDER_SIZE_USD = 20.0      # Max $20 per order
MAX_DAILY_LOSS_USD = 40.0      # Stop trading if daily loss > $40
MAX_OPEN_POSITIONS = 8         # Max concurrent live positions
MIN_BOOK_DEPTH_USD = 50.0      # Min orderbook depth to trade


def _get_client() -> ClobClient:
    cfg = get_config()
    creds = ApiCreds(
        api_key=cfg.clob_api_key,
        api_secret=cfg.clob_api_secret,
        api_passphrase=cfg.clob_api_passphrase,
    )
    return ClobClient(
        "https://clob.polymarket.com",
        key=cfg.private_key,
        chain_id=137,
        creds=creds,
        signature_type=0,
    )


def get_balance() -> float:
    """Get available CLOB USDC.e balance."""
    client = _get_client()
    bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=0)
    )
    return int(bal["balance"]) / 1e6


def get_open_orders():
    """Get all open orders."""
    client = _get_client()
    return client.get_orders()


def _live_positions_file() -> Path:
    return get_config().live_positions_file


def _live_history_file() -> Path:
    return get_config().live_history_file


def get_live_positions() -> list:
    """Load live positions from file."""
    f = _live_positions_file()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def _save_live_positions(positions: list):
    _live_positions_file().write_text(json.dumps(positions, indent=2, ensure_ascii=False))


def _append_live_history(trade: dict):
    f = _live_history_file()
    history = []
    if f.exists():
        try:
            history = json.loads(f.read_text())
        except Exception:
            pass
    history.append(trade)
    history = history[-200:]
    f.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def _notify_trade(position: dict, action: str):
    """Write trade notification for Discord alert pickup."""
    try:
        notifications = []
        if TRADE_NOTIFY_FILE.exists():
            try:
                notifications = json.loads(TRADE_NOTIFY_FILE.read_text())
            except Exception:
                notifications = []
        
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
        
        notifications.append({
            "message": msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
        })
        
        TRADE_NOTIFY_FILE.write_text(json.dumps(notifications, indent=2, ensure_ascii=False))
    except Exception as e:
        console.print(f"[yellow]  ⚠ Notify error: {e}[/yellow]")


def _check_daily_loss() -> float:
    """Calculate today's realized loss."""
    f = _live_history_file()
    if not f.exists():
        return 0.0
    try:
        history = json.loads(f.read_text())
    except Exception:
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = sum(
        t.get("pnl", 0) for t in history
        if t.get("exit_time", "").startswith(today)
    )
    return daily_pnl


def place_limit_order(
    token_id: str,
    side: str,        # "BUY"
    price: float,
    size: float,       # in shares (not USD)
    neg_risk: bool = False,
) -> dict | None:
    """Place a limit order on Polymarket CLOB.
    
    Bumps price by 2 cents to cross the spread and get immediate fills.
    Returns order result dict or None on failure.
    """
    # Check orderbook and price at best ask to guarantee fill
    try:
        client_check = _get_client()
        book = client_check.get_order_book(token_id)
        if side == "BUY" and hasattr(book, 'asks') and book.asks:
            best_ask = float(book.asks[0].price)
            spread = best_ask - price
            # If spread > 10%, market is dead — skip
            if spread > 0.10:
                console.print(f"[yellow]  ⚠ Wide spread: mid={price:.3f} ask={best_ask:.3f} (spread={spread:.3f}) — skipping dead market[/yellow]")
                return None
            # Price at best ask to guarantee immediate fill
            price = best_ask
            console.print(f"[dim]  📊 Pricing at best ask: ${price:.3f}[/dim]")
        elif side == "SELL" and hasattr(book, 'bids') and book.bids:
            best_bid = float(book.bids[0].price)
            spread = price - best_bid
            if spread > 0.10:
                console.print(f"[yellow]  ⚠ Wide spread — skipping dead market[/yellow]")
                return None
            price = best_bid
    except Exception as e:
        console.print(f"[dim]  ⚠ Orderbook check failed: {e}, using original price + 2c bump[/dim]")
        if side == "BUY":
            price = min(round(price + 0.02, 4), 0.99)
        else:
            price = max(round(price - 0.02, 4), 0.01)
    
    size = int(size)  # ensure integer shares
    cost = size * price
    
    # Safety checks
    if cost > MAX_ORDER_SIZE_USD:
        console.print(f"[red]  ❌ Order too large: ${cost:.2f} > ${MAX_ORDER_SIZE_USD}[/red]")
        return None
    
    daily_loss = _check_daily_loss()
    if daily_loss < -MAX_DAILY_LOSS_USD:
        console.print(f"[red]  ❌ Daily loss limit hit: ${daily_loss:.2f}[/red]")
        return None
    
    balance = get_balance()
    if cost > balance * 0.95:  # Leave 5% buffer
        console.print(f"[red]  ❌ Insufficient balance: ${balance:.2f} < ${cost:.2f}[/red]")
        return None
    
    positions = get_live_positions()
    open_pos = [p for p in positions if p.get("status") == "open"]
    if len(open_pos) >= MAX_OPEN_POSITIONS:
        console.print(f"[red]  ❌ Max positions ({MAX_OPEN_POSITIONS}) reached[/red]")
        return None
    
    client = _get_client()
    
    try:
        # Create and post order
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side,
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, OrderType.GTC)
        
        console.print(f"[bold green]  ✅ LIVE ORDER: {side} {size} shares @ ${price} (${cost:.2f})[/bold green]")
        console.print(f"[dim]  Order ID: {result.get('orderID', '?')}[/dim]")
        
        return result
    except Exception as e:
        console.print(f"[red]  ❌ Order failed: {e}[/red]")
        return None


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
    if shares < 2:
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
    initial_status = "pending"  # Default: not yet filled
    filled_shares = 0
    try:
        client = _get_client()
        order_info = client.get_order(order_id)
        size_matched = float(order_info.get("size_matched", 0))
        original_size = float(order_info.get("original_size", shares))
        if size_matched >= original_size * 0.95:  # ≥95% filled = open
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
        "status": initial_status,
        "order_id": order_id,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "trigger_news": trigger_news[:200],
        "neg_risk": neg_risk,
    }
    
    positions = get_live_positions()
    positions.append(position)
    _save_live_positions(positions)
    
    # Write trade notification for Discord alert cron
    _notify_trade(position, "OPEN")
    
    return position


def close_live_position(position: dict, reason: str = "manual") -> dict | None:
    """Close a live position by selling shares."""
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
    
    # Place sell order at best bid
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
        
        # Update position
        position["status"] = "closed"
        position["exit_price"] = best_bid
        position["exit_time"] = datetime.now(timezone.utc).isoformat()
        position["exit_reason"] = reason
        position["pnl"] = pnl
        
        # Save
        positions = get_live_positions()
        for i, p in enumerate(positions):
            if p["id"] == position["id"]:
                positions[i] = position
                break
        _save_live_positions(positions)
        _append_live_history(position)
        
        icon = "🟢" if pnl >= 0 else "🔴"
        console.print(f"  {icon} CLOSED LIVE [{reason}]: ${pnl:+.2f} ({entry:.1%}→{best_bid:.1%})")
        
        _notify_trade(position, "CLOSE")
        
        # Journal Step 5: Settlement
        trade_id = position.get("trade_id", "")
        if trade_id:
            try:
                entry_dt = datetime.fromisoformat(position.get("entry_time", ""))
                hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                hours = 0
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
    positions = get_live_positions()
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
            order_status = order_info.get("status", "")  # LIVE, MATCHED, CANCELLED
            
            if size_matched >= original_size * 0.95 or order_status == "MATCHED":
                # Fully filled → open
                pos["status"] = "open"
                pos["filled_shares"] = int(size_matched)
                pos["cost"] = round(size_matched * pos["entry_price"], 2)
                console.print(f"[green]  ✅ Order filled: {pos['question'][:40]}... ({int(size_matched)} shares)[/green]")
                _notify_trade(pos, "FILL")
                # Journal
                trade_id = pos.get("trade_id", "")
                if trade_id:
                    try:
                        record_fill(trade_id, pos["entry_price"], int(size_matched), pos["cost"])
                    except Exception:
                        pass
                updated += 1
                
            elif size_matched > pos.get("filled_shares", 0):
                # More shares filled
                pos["status"] = "partial"
                pos["filled_shares"] = int(size_matched)
                console.print(f"[yellow]  ⏳ Partial fill: {pos['question'][:40]}... ({int(size_matched)}/{int(original_size)})[/yellow]")
                
            elif order_status == "CANCELLED":
                # Order cancelled (expired, manually cancelled, etc.)
                pos["status"] = "cancelled"
                console.print(f"[red]  ❌ Order cancelled: {pos['question'][:40]}...[/red]")
                trade_id = pos.get("trade_id", "")
                if trade_id:
                    try:
                        add_event(trade_id, "order_cancelled", "Order cancelled on CLOB")
                    except Exception:
                        pass
                updated += 1
                
            else:
                # Still pending — check timeout (cancel after 2h if unfilled)
                if pos.get("entry_time"):
                    entry_dt = datetime.fromisoformat(pos["entry_time"])
                    hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                    if hours > 2 and size_matched == 0:
                        # Cancel stale unfilled order
                        try:
                            client.cancel(order_id)
                            pos["status"] = "cancelled"
                            console.print(f"[yellow]  ⏰ Cancelled stale order (2h unfilled): {pos['question'][:40]}...[/yellow]")
                            updated += 1
                        except Exception:
                            pass
                            
        except Exception as e:
            console.print(f"[dim]  Could not check order {order_id[:12]}: {e}[/dim]")
    
    # Save updated positions (remove cancelled)
    positions = [p for p in positions if p.get("status") != "cancelled"]
    _save_live_positions(positions)
    return updated


def check_live_exits() -> int:
    """Check live positions for exit conditions. Returns count of closed positions."""
    import httpx
    
    # First check pending orders for fills
    check_pending_orders()
    
    positions = get_live_positions()
    open_pos = [p for p in positions if p.get("status") == "open"]
    if not open_pos:
        return 0
    
    closed = 0
    for pos in open_pos:
        market_id = pos["market_id"]
        
        # Get current price from CLOB
        try:
            client = _get_client()
            market = client.get_market(market_id)
            tokens = market.get("tokens", [])
            
            # Find our token's current price
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
        
        # Check timeout (6h for sniper)
        if pos.get("entry_time"):
            entry_dt = datetime.fromisoformat(pos["entry_time"])
            hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            if hours > 6:
                result = close_live_position(pos, "TIMEOUT")
                if result:
                    closed += 1
                continue
    
    return closed


def cleanup_stale_orders() -> int:
    """Cancel stale orders: >12h old, market expiring <1h, or price drifted >20%.
    Returns count of cancelled orders."""
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
        
        # 1. Timeout: >12h since creation
        if created > 0:
            age_hours = (now.timestamp() - created) / 3600
            if age_hours > 12:
                reason = f"超时({age_hours:.0f}h)"
        
        # 2. Market expiring soon or price drifted
        if not reason and market_id:
            try:
                mdata = client.get_market(market_id)
                tokens = mdata.get("tokens", [])
                
                # Check expiry
                end_date = mdata.get("end_date_iso", "") or ""
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_left = (end_dt - now).total_seconds() / 3600
                        if hours_left < 1:
                            reason = f"即将到期({hours_left:.1f}h)"
                    except Exception:
                        pass
                
                # Check price drift >20%
                if not reason:
                    for t in tokens:
                        if t.get("outcome") == outcome:
                            current = float(t.get("price", 0))
                            if current > 0 and price > 0:
                                drift = abs(current - price) / price
                                if drift > 0.20:
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
            
            # Notify
            try:
                notifications = []
                if TRADE_NOTIFY_FILE.exists():
                    try:
                        notifications = json.loads(TRADE_NOTIFY_FILE.read_text())
                    except Exception:
                        pass
                notifications.append({
                    "message": f"🗑️ 自动撤单: {o.get('outcome','')} @${float(o.get('price',0)):.2f} | 原因: {reason} | 释放${cost:.1f}",
                    "timestamp": now.isoformat(),
                    "action": "CANCEL",
                })
                TRADE_NOTIFY_FILE.write_text(json.dumps(notifications, indent=2, ensure_ascii=False))
            except Exception:
                pass
            
            cancelled += 1
            time.sleep(0.5)
        except Exception as e:
            console.print(f"[red]  ❌ 撤单失败 {order_id[:16]}: {e}[/red]")
    
    return cancelled


def release_funds_for_signal(needed_usd: float) -> float:
    """Cancel oldest orders to free up funds for a new signal.
    Returns amount freed."""
    client = _get_client()
    balance = get_balance()
    
    if balance >= needed_usd:
        return balance  # Already enough
    
    orders = client.get_orders()
    if not orders:
        return balance
    
    # Sort by creation time (oldest first)
    orders.sort(key=lambda o: int(o.get("created_at", 0)))
    
    freed = 0.0
    for o in orders:
        if balance + freed >= needed_usd:
            break
        
        order_id = o.get("id", "")
        cost = float(o.get("original_size", 0)) * float(o.get("price", 0))
        
        try:
            client.cancel(order_id)
            freed += cost
            console.print(f"[yellow]  🔓 释放资金: 撤单 {o.get('outcome','')} @${float(o.get('price',0)):.2f} → +${cost:.1f}[/yellow]")
            
            # Notify
            try:
                notifications = []
                if TRADE_NOTIFY_FILE.exists():
                    try:
                        notifications = json.loads(TRADE_NOTIFY_FILE.read_text())
                    except Exception:
                        pass
                notifications.append({
                    "message": f"🔓 为新信号释放资金: 撤单 {o.get('outcome','')} → +${cost:.1f}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "RELEASE",
                })
                TRADE_NOTIFY_FILE.write_text(json.dumps(notifications, indent=2, ensure_ascii=False))
            except Exception:
                pass
            
            time.sleep(0.5)
        except Exception:
            continue
    
    return balance + freed


def auto_redeem_resolved() -> float:
    """Check for resolved markets with CTF tokens and redeem them.
    Returns total USDC.e redeemed."""
    import httpx
    from web3 import Web3
    
    cfg = get_config()

    # Find RPCs
    for rpc in [cfg.rpc_url, 'https://polygon.meowrpc.com']:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                break
        except Exception:
            continue
    else:
        console.print("[red]  ❌ No Polygon RPC available for redeem[/red]")
        return 0.0
    
    account = w3.to_checksum_address(cfg.wallet_address)
    collateral = w3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
    ctf_addr = w3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
    
    ctf_abi = [{
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"
    }]
    balance_abi = [{'inputs':[{'name':'','type':'address'},{'name':'','type':'uint256'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'stateMutability':'view','type':'function'}]
    
    ctf = w3.eth.contract(address=ctf_addr, abi=ctf_abi)
    ctf_balance = w3.eth.contract(address=ctf_addr, abi=balance_abi)
    
    # Get all trades to find condition IDs
    client = _get_client()
    trades = client.get_trades()
    
    redeemed_total = 0.0
    seen_conditions = set()
    
    for trade in trades:
        condition_id = trade.get('market', '')
        if condition_id in seen_conditions:
            continue
        seen_conditions.add(condition_id)
        
        # Check if market is resolved
        try:
            r = httpx.get(f'https://clob.polymarket.com/markets/{condition_id}', timeout=10)
            mdata = r.json()
            tokens = mdata.get('tokens', [])
            
            # Market resolved if any outcome price = 0 or 1
            resolved = any(float(t.get('price', 0.5)) in (0, 1) for t in tokens)
            if not resolved:
                continue
            
            # Check if we have any CTF tokens for this market
            has_tokens = False
            for t in tokens:
                token_id = int(t.get('token_id', '0'))
                if token_id == 0:
                    continue
                bal = ctf_balance.functions.balanceOf(account, token_id).call()
                if bal > 0:
                    has_tokens = True
                    break
            
            if not has_tokens:
                continue
            
            console.print(f"[cyan]  🔄 Redeeming: {mdata.get('question', '?')[:50]}[/cyan]")
            
            cid_bytes = bytes.fromhex(condition_id[2:]) if condition_id.startswith('0x') else bytes.fromhex(condition_id)
            nonce = w3.eth.get_transaction_count(account, 'pending')
            
            tx = ctf.functions.redeemPositions(
                collateral, b'\x00' * 32, cid_bytes, [1, 2]
            ).build_transaction({
                'from': account, 'nonce': nonce, 'gas': 300000,
                'maxFeePerGas': w3.to_wei(100, 'gwei'),
                'maxPriorityFeePerGas': w3.to_wei(50, 'gwei'),
                'chainId': 137
            })
            
            signed = w3.eth.account.sign_transaction(tx, cfg.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt['status'] == 1:
                console.print(f"[green]  ✅ Redeemed! tx={tx_hash.hex()[:16]}...[/green]")
                redeemed_total += 1
                # Notify redeem
                try:
                    notifications = []
                    if TRADE_NOTIFY_FILE.exists():
                        try:
                            notifications = json.loads(TRADE_NOTIFY_FILE.read_text())
                        except Exception:
                            pass
                    notifications.append({
                        "message": f"🔄 赎回成功: {mdata.get('question','?')[:50]} → USDC.e已回到钱包\ntx: {tx_hash.hex()[:20]}...",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "action": "REDEEM",
                    })
                    TRADE_NOTIFY_FILE.write_text(json.dumps(notifications, indent=2, ensure_ascii=False))
                except Exception:
                    pass
            else:
                console.print(f"[red]  ❌ Redeem failed for {condition_id[:20]}[/red]")
                
        except Exception as e:
            console.print(f"[yellow]  ⚠ Redeem check error: {e}[/yellow]")
            continue
    
    return redeemed_total


# ── Test order ──
if __name__ == "__main__":
    print(f"Balance: ${get_balance():.2f}")
    print(f"Open orders: {len(get_open_orders())}")
    print(f"Live positions: {len(get_live_positions())}")

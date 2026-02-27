"""Order executor — pure CLOB order placement/cancellation."""

import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from rich.console import Console

from config import get_config
from db import get_positions, get_daily_pnl, add_notification

console = Console()



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


def place_limit_order(
    token_id: str,
    side: str,
    price: float,
    size: float,
    neg_risk: bool = False,
) -> dict | None:
    """Place a limit order on Polymarket CLOB.

    Prices at best ask/bid to guarantee immediate fills.
    Returns order result dict or None on failure.
    """
    try:
        client_check = _get_client()
        book = client_check.get_order_book(token_id)
        if side == "BUY" and hasattr(book, 'asks') and book.asks:
            best_ask = float(book.asks[0].price)
            spread = best_ask - price
            if spread > 0.10:
                console.print(f"[yellow]  ⚠ Wide spread: mid={price:.3f} ask={best_ask:.3f} (spread={spread:.3f}) — skipping dead market[/yellow]")
                return None
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
        console.print(f"[dim]  ⚠ Orderbook check failed (using original price + 2c bump)[/dim]")
        if side == "BUY":
            price = min(round(price + 0.02, 4), 0.99)
        else:
            price = max(round(price - 0.02, 4), 0.01)

    size = int(size)
    cost = size * price

    # Safety checks (all limits from config.yaml)
    cfg = get_config()
    if cost > cfg.max_order_size:
        console.print(f"[red]  ❌ Order too large: ${cost:.2f} > ${cfg.max_order_size}[/red]")
        return None

    daily_loss = get_daily_pnl(mode="live")
    if daily_loss < -cfg.daily_loss_limit:
        console.print(f"[red]  ❌ Daily loss limit hit: ${daily_loss:.2f}[/red]")
        return None

    balance = get_balance()
    if cost > balance * 0.95:
        console.print(f"[red]  ❌ Insufficient balance: ${balance:.2f} < ${cost:.2f}[/red]")
        return None

    open_pos = get_positions(mode="live", status="open")
    if len(open_pos) >= cfg.max_positions:
        console.print(f"[red]  ❌ Max positions ({cfg.max_positions}) reached[/red]")
        return None

    client = _get_client()

    try:
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
    except Exception:
        console.print("[red]  ❌ Order failed[/red]")
        return None


def release_funds_for_signal(needed_usd: float) -> float:
    """Cancel oldest orders to free up funds for a new signal.
    Returns amount freed."""
    client = _get_client()
    balance = get_balance()

    if balance >= needed_usd:
        return balance

    orders = client.get_orders()
    if not orders:
        return balance

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
            add_notification(
                f"🔓 为新信号释放资金: 撤单 {o.get('outcome','')} → +${cost:.1f}",
                "RELEASE",
            )
            time.sleep(0.5)
        except Exception:
            continue

    return balance + freed

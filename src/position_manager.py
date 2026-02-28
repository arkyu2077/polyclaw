"""Position management and exit strategy for paper trading."""

import json
import uuid
import httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field

from rich.console import Console
from rich.table import Table
from rich import box

from .config import get_config
from .db import get_positions, upsert_position, insert_trade, get_trades

console = Console()

def kelly_size(ai_probability: float, entry_price: float, confidence: float, bankroll: float) -> float:
    """
    Half-Kelly position sizing for binary prediction markets.

    NOTE: ai_probability should ALREADY be discounted (by probability_engine).
    AI discount (50%) is applied upstream when generating estimates.

    FIX: Previous version shrunk toward 0.5 which is wrong for extreme-priced
    markets. Now we blend AI estimate toward ENTRY PRICE (market consensus),
    not toward 50%.
    """
    cfg = get_config()
    blend_factor = max(0.5, min(1.0, confidence))
    p = entry_price + (ai_probability - entry_price) * blend_factor

    p = max(0.01, min(0.99, p))
    q = 1 - p

    net_profit_per_share = (1 - entry_price) * (1 - cfg.fee_rate)
    if entry_price <= 0.001 or net_profit_per_share <= 0:
        return 0
    b = net_profit_per_share / entry_price

    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0

    f_half = f_star * cfg.kelly_fraction
    f_capped = min(f_half, cfg.max_position_pct)

    return round(bankroll * f_capped, 2)


@dataclass
class Position:
    id: str
    market_id: str
    question: str
    direction: str
    entry_price: float
    shares: int
    cost: float
    entry_time: str
    target_price: float = 0.0
    stop_loss: float = 0.0
    status: str = "open"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    trigger_news: str = ""
    confidence: float = 0.7
    peak_price: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def to_db_dict(self) -> dict:
        """Convert to db-compatible dict with mode field."""
        d = self.to_dict()
        d["mode"] = "paper"
        d["strategy"] = ""
        d["token_id"] = ""
        d["filled_shares"] = self.shares
        d["order_id"] = ""
        d["neg_risk"] = 0
        d.setdefault("trade_id", "")
        return d


def _load_positions() -> list[Position]:
    rows = get_positions(mode="paper")
    result = []
    for r in rows:
        try:
            result.append(Position.from_dict(r))
        except Exception:
            pass
    return result


def _save_positions(positions: list[Position]):
    for p in positions:
        upsert_position(p.to_db_dict())


def _append_history(position: Position):
    d = position.to_dict()
    try:
        entry_dt = datetime.fromisoformat(d.get("entry_time", ""))
        exit_dt = datetime.fromisoformat(d.get("exit_time", ""))
        hours = (exit_dt - entry_dt).total_seconds() / 3600
    except (ValueError, TypeError):
        hours = 0

    insert_trade({
        "position_id": d["id"],
        "mode": "paper",
        "strategy": "",
        "market_id": d.get("market_id"),
        "question": d.get("question"),
        "direction": d.get("direction"),
        "entry_price": d.get("entry_price"),
        "exit_price": d.get("exit_price"),
        "shares": d.get("shares"),
        "cost": d.get("cost"),
        "pnl": d.get("pnl"),
        "fees": 0,
        "entry_time": d.get("entry_time"),
        "exit_time": d.get("exit_time"),
        "exit_reason": d.get("exit_reason"),
        "trigger_news": d.get("trigger_news"),
        "confidence": d.get("confidence"),
        "hold_hours": round(hours, 2),
    })


def _fetch_market_price(market_id: str) -> float | None:
    """Fetch fresh price for a market from Gamma API.
    Handles both numeric IDs and condition_id (long hex) formats."""
    headers = {"User-Agent": "Mozilla/5.0"}

    urls = [f"https://gamma-api.polymarket.com/markets?id={market_id}"]
    if len(str(market_id)) > 20:
        urls = [f"https://gamma-api.polymarket.com/markets?condition_id={market_id}"]

    for url in urls:
        try:
            resp = httpx.get(url, timeout=15, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                data = data[0]
            prices = data.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if prices:
                return float(prices[0])
        except Exception:
            continue

    return None


def open_position(
    market_id: str,
    question: str,
    direction: str,
    entry_price: float,
    ai_probability: float,
    bankroll: float,
    trigger_news: str = "",
    confidence: float = 0.7,
) -> Position | None:
    """Open a new paper position using Half-Kelly sizing. Returns Position or None."""
    cfg = get_config()
    positions = _load_positions()
    open_positions = [p for p in positions if p.status == "open"]
    now = datetime.now(timezone.utc)

    if len(open_positions) >= cfg.max_positions:
        console.print(f"[yellow]  ⚠ Max positions ({cfg.max_positions}) reached, skipping[/yellow]")
        return None

    if any(p.market_id == market_id for p in open_positions):
        console.print(f"[yellow]  ⚠ Already have position on this market, skipping[/yellow]")
        return None

    # Cooldown check — use trades table
    closed_trades = get_trades(mode="paper")
    for ct in closed_trades:
        if ct.get("market_id") == market_id and ct.get("exit_time"):
            try:
                exit_dt = datetime.fromisoformat(ct["exit_time"])
                if now - exit_dt < timedelta(hours=cfg.cooldown_hours):
                    console.print(f"[yellow]  ⚠ Cooldown active for this market (exited {ct['exit_time']})[/yellow]")
                    return None
            except (ValueError, TypeError):
                pass

    kelly_cost = kelly_size(ai_probability, entry_price, confidence, bankroll)
    if kelly_cost < 1.0:
        console.print(f"[yellow]  ⚠ Kelly says no edge (ai={ai_probability:.1%} vs price={entry_price:.1%}, conf={confidence:.0%}) → $0[/yellow]")
        return None

    total_invested = sum(p.cost for p in open_positions)
    max_remaining = bankroll * cfg.max_exposure_pct - total_invested
    allowed_cost = min(kelly_cost, max(0, max_remaining))

    if allowed_cost < 1.0:
        console.print(f"[yellow]  ⚠ Exposure limit reached (${total_invested:.0f} invested)[/yellow]")
        return None

    shares = int(allowed_cost / entry_price) if entry_price > 0 else 0
    if shares < 3:
        console.print(f"[yellow]  ⚠ Position too small ({shares} shares, ${allowed_cost:.1f}), skipping[/yellow]")
        return None
    cost = round(shares * entry_price, 2)

    adj_p = 0.5 + (ai_probability - 0.5) * confidence
    console.print(f"[dim]  📐 Kelly: p={adj_p:.1%} c={entry_price:.1%} → half-kelly=${kelly_cost:.0f}, actual=${cost:.0f}[/dim]")

    if confidence >= 0.75:
        tp_ratio = cfg.high_conf_tp_ratio
    elif confidence <= 0.55:
        tp_ratio = cfg.low_conf_tp_ratio
    else:
        tp_ratio = cfg.tp_ratio

    target_price = round(entry_price + (ai_probability - entry_price) * tp_ratio, 4)

    kelly_pct = cost / bankroll
    if kelly_pct <= 0.03:
        sl_ratio = cfg.wide_sl_ratio
    elif kelly_pct >= 0.10:
        sl_ratio = cfg.tight_sl_ratio
    else:
        sl_ratio = cfg.sl_ratio

    stop_loss = round(entry_price * sl_ratio, 4)

    console.print(f"[dim]  📊 Exit: TP@{target_price:.1%}({tp_ratio:.0%}move) SL@{stop_loss:.1%}({1-sl_ratio:.0%}loss) conf={confidence:.0%}[/dim]")

    pos = Position(
        id=str(uuid.uuid4())[:8],
        market_id=market_id,
        question=question,
        direction=direction,
        entry_price=entry_price,
        shares=shares,
        cost=cost,
        target_price=target_price,
        stop_loss=stop_loss,
        entry_time=now.isoformat(),
        trigger_news=trigger_news[:200],
        confidence=confidence,
        peak_price=entry_price,
    )

    upsert_position(pos.to_db_dict())
    console.print(f"[bold green]  ✅ OPENED position: {direction} {shares} shares @ {entry_price:.1%} → target {target_price:.1%} / stop {stop_loss:.1%}[/bold green]")
    return pos


def _close_position(pos: Position, exit_price: float, reason: str):
    """Close a position and record PnL."""
    pos.status = "closed"
    pos.exit_price = exit_price
    pos.exit_time = datetime.now(timezone.utc).isoformat()
    pos.exit_reason = reason

    if pos.direction == "BUY_YES":
        pos.pnl = round((exit_price - pos.entry_price) * pos.shares, 2)
    else:
        pos.pnl = round((pos.entry_price - exit_price) * pos.shares, 2)

    upsert_position(pos.to_db_dict())
    _append_history(pos)
    _log_calibration(pos)
    icon = "🟢" if (pos.pnl or 0) >= 0 else "🔴"
    console.print(
        f"  {icon} CLOSED [{reason}]: {pos.question[:40]} | "
        f"PnL: ${pos.pnl:+.2f} ({pos.entry_price:.1%}→{exit_price:.1%})"
    )


def _log_calibration(pos: Position):
    """Record AI prediction vs actual outcome for calibration analysis.
    Now stored as a trade in the trades table — can be queried from there."""
    # Calibration data is embedded in the trades table (confidence, pnl, exit_reason, etc.)
    # No separate file needed; the insert_trade in _append_history covers it.
    pass


def check_exits() -> int:
    """Check all open positions for exit conditions with trailing stop.

    Exit priority:
    1. Take profit — hit target price
    2. Stop loss — hit stop price
    3. Trailing stop — price dropped 30% from peak (after 50% of target move)
    4. Timeout — 24h with <2% move
    5. Time acceleration — tighten stops as market expiration approaches
    """
    positions = _load_positions()
    open_positions = [p for p in positions if p.status == "open"]
    if not open_positions:
        return 0

    now = datetime.now(timezone.utc)
    closed_count = 0

    for pos in open_positions:
        yes_price = _fetch_market_price(pos.market_id)
        if yes_price is None:
            continue

        if pos.direction == "BUY_YES":
            current = yes_price
        else:
            current = 1 - yes_price

        peak = pos.peak_price or pos.entry_price
        if current > peak:
            pos.peak_price = current
            peak = current

        # 1. Take profit
        if current >= pos.target_price:
            _close_position(pos, current, "TAKE_PROFIT")
            closed_count += 1
            continue

        # 2. Stop loss
        if current <= pos.stop_loss:
            _close_position(pos, current, "STOP_LOSS")
            closed_count += 1
            continue

        # 3. Trailing stop
        cfg = get_config()
        target_move = pos.target_price - pos.entry_price
        if target_move > 0:
            progress = (peak - pos.entry_price) / target_move
            if progress >= cfg.trailing_stop_activation:
                trail_stop = peak * (1 - cfg.trailing_stop_distance)
                effective_trail = max(trail_stop, pos.stop_loss)
                if current <= effective_trail:
                    profit_locked = (current - pos.entry_price) * pos.shares
                    _close_position(pos, current, f"TRAILING_STOP(peak={peak:.1%},locked=${profit_locked:+.1f})")
                    closed_count += 1
                    continue

        # 4. Time-based stop tightening
        entry_dt = datetime.fromisoformat(pos.entry_time)
        age_hours = (now - entry_dt).total_seconds() / 3600

        if age_hours > 12 and age_hours <= 24:
            tightened_stop = (pos.stop_loss + pos.entry_price) / 2
            if current <= tightened_stop:
                _close_position(pos, current, "TIME_TIGHTENED_STOP")
                closed_count += 1
                continue

        # 5. Timeout
        if age_hours > cfg.timeout_hours:
            move = abs(current - pos.entry_price)
            if move < cfg.timeout_move_threshold:
                _close_position(pos, current, "TIMEOUT_FLAT")
                closed_count += 1
                continue
            else:
                _close_position(pos, current, "TIMEOUT_AGED")
                closed_count += 1
                continue

        # Save peak price update
        upsert_position(pos.to_db_dict())

    return closed_count


def get_summary(bankroll: float = 1000.0) -> dict:
    """Get portfolio summary stats."""
    cfg = get_config()
    positions = _load_positions()
    open_pos = [p for p in positions if p.status == "open"]
    closed_trades = get_trades(mode="paper")

    total_invested = sum(p.cost for p in open_pos)
    winners = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    realized_pnl = sum(t.get("pnl") or 0 for t in closed_trades)
    win_rate = len(winners) / len(closed_trades) if closed_trades else 0

    return {
        "open_count": len(open_pos),
        "max_positions": cfg.max_positions,
        "total_invested": total_invested,
        "exposure_pct": total_invested / bankroll if bankroll > 0 else 0,
        "closed_count": len(closed_trades),
        "realized_pnl": realized_pnl,
        "win_rate": win_rate,
        "open_positions": open_pos,
        "closed_positions": [Position.from_dict(t) for t in closed_trades if "entry_price" in t],
    }


def display_positions(bankroll: float = 1000.0):
    """Rich display of current positions and summary."""
    summary = get_summary(bankroll)
    open_pos = summary["open_positions"]
    now = datetime.now(timezone.utc)

    console.print()
    console.print(f"[bold]📊 Open Positions ({summary['open_count']}/{summary['max_positions']})[/bold]")

    if open_pos:
        table = Table(box=box.SIMPLE_HEAVY)
        table.add_column("Market", max_width=30)
        table.add_column("Dir", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Target", justify="right")
        table.add_column("Stop", justify="right")
        table.add_column("PnL", justify="right")
        table.add_column("Age", justify="right")

        for pos in open_pos:
            yes_price = _fetch_market_price(pos.market_id)
            if pos.direction == "BUY_YES":
                current = yes_price or pos.entry_price
            else:
                current = (1 - yes_price) if yes_price is not None else pos.entry_price

            pnl_pct = (current - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
            pnl_color = "green" if pnl_pct >= 0 else "red"

            entry_dt = datetime.fromisoformat(pos.entry_time)
            age = now - entry_dt
            age_str = f"{int(age.total_seconds()//3600)}h{int((age.total_seconds()%3600)//60)}m"

            table.add_row(
                pos.question[:30],
                pos.direction.replace("BUY_", ""),
                f"{pos.entry_price*100:.1f}¢",
                f"{current*100:.1f}¢",
                f"{pos.target_price*100:.1f}¢",
                f"{pos.stop_loss*100:.1f}¢",
                f"[{pnl_color}]{pnl_pct:+.1f}%[/{pnl_color}]",
                age_str,
            )
        console.print(table)
    else:
        console.print("  [dim]No open positions[/dim]")

    # Closed today
    today = now.date()
    closed_trades = get_trades(mode="paper")
    closed_today = [t for t in closed_trades
                    if t.get("exit_time") and datetime.fromisoformat(t["exit_time"]).date() == today]
    today_pnl = sum(t.get("pnl") or 0 for t in closed_today)
    today_winners = len([t for t in closed_today if (t.get("pnl") or 0) > 0])
    today_wr = today_winners / len(closed_today) * 100 if closed_today else 0

    console.print(
        f"\n💰 Closed Today: {len(closed_today)} trades | "
        f"Win rate: {today_wr:.1f}% | "
        f"Net PnL: [{'green' if today_pnl >= 0 else 'red'}]${today_pnl:+.2f}[/]"
    )
    console.print(
        f"📈 All Time: {summary['closed_count']} trades | "
        f"Win rate: {summary['win_rate']*100:.1f}% | "
        f"Realized PnL: [{'green' if summary['realized_pnl'] >= 0 else 'red'}]${summary['realized_pnl']:+.2f}[/]"
    )
    console.print()

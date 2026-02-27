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

console = Console()

DATA_DIR = Path(__file__).parent
POSITIONS_FILE = DATA_DIR / "positions.json"
HISTORY_FILE = DATA_DIR / "trade_history.json"

# Risk parameters
MAX_OPEN_POSITIONS = 8
MAX_POSITION_PCT = 0.15      # 15% bankroll per position (hard cap even if Kelly says more)
MAX_TOTAL_EXPOSURE_PCT = 1.00  # 100% total — Kelly controls sizing
COOLDOWN_HOURS = 1
TIMEOUT_HOURS = 24
TIMEOUT_MOVE_THRESHOLD = 0.02  # <2% move = "no movement"

# Exit strategy parameters
BASE_TP_RATIO = 0.70         # Base: take 70% of expected move
HIGH_CONF_TP_RATIO = 0.85    # High confidence (>0.75): take 85%
LOW_CONF_TP_RATIO = 0.55     # Low confidence (<0.55): take 55%
BASE_SL_RATIO = 0.75         # Base: stop at 25% loss
WIDE_SL_RATIO = 0.65         # Small Kelly positions: wider stop (35% loss)
TIGHT_SL_RATIO = 0.82        # Large Kelly positions: tighter stop (18% loss)
TRAILING_STOP_ACTIVATION = 0.5  # Activate trailing stop after 50% of target move
TRAILING_STOP_DISTANCE = 0.30   # Trail at 30% below peak
FEE_RATE = 0.003             # ~0.3% spread cost only (most markets are fee-free!)
KELLY_FRACTION = 0.5         # Half-Kelly for safety
def kelly_size(ai_probability: float, entry_price: float, confidence: float, bankroll: float) -> float:
    """
    Half-Kelly position sizing for binary prediction markets.
    
    NOTE: ai_probability should ALREADY be discounted (by probability_engine).
    AI discount (50%) is applied upstream when generating estimates.
    
    FIX: Previous version shrunk toward 0.5 which is wrong for extreme-priced
    markets. Now we blend AI estimate toward ENTRY PRICE (market consensus),
    not toward 50%.
    """
    # Blend AI estimate toward market price (entry_price) based on confidence
    # High confidence = trust AI more; low confidence = trust market more
    blend_factor = max(0.5, min(1.0, confidence))
    p = entry_price + (ai_probability - entry_price) * blend_factor
    
    # Clamp to reasonable range
    p = max(0.01, min(0.99, p))
    q = 1 - p
    
    # Net odds after fees
    # Allow entries down to 0.1% ($0.001)
    net_profit_per_share = (1 - entry_price) * (1 - FEE_RATE)
    if entry_price <= 0.001 or net_profit_per_share <= 0:
        return 0
    b = net_profit_per_share / entry_price
    
    # Full Kelly
    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0  # No edge — don't bet
    
    # Half Kelly
    f_half = f_star * KELLY_FRACTION
    
    # Cap at MAX_POSITION_PCT
    f_capped = min(f_half, MAX_POSITION_PCT)
    
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
    target_price: float
    stop_loss: float
    entry_time: str
    status: str = "open"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    trigger_news: str = ""
    confidence: float = 0.7
    peak_price: float | None = None  # Highest price seen (for trailing stop)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        # Handle any extra/missing fields gracefully
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})


def _load_positions() -> list[Position]:
    if not POSITIONS_FILE.exists():
        return []
    try:
        data = json.loads(POSITIONS_FILE.read_text())
        return [Position.from_dict(d) for d in data]
    except Exception:
        return []


def _save_positions(positions: list[Position]):
    POSITIONS_FILE.write_text(json.dumps([p.to_dict() for p in positions], indent=2))


def _append_history(position: Position):
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = []
    history.append(position.to_dict())
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _fetch_market_price(market_id: str) -> float | None:
    """Fetch fresh price for a market from Gamma API.
    Handles both numeric IDs and condition_id (long hex) formats."""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # Try numeric ID first, then condition_id
    urls = [f"https://gamma-api.polymarket.com/markets?id={market_id}"]
    if len(str(market_id)) > 20:
        # Long ID = likely condition_id, try that param instead
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
                return float(prices[0])  # YES price
        except Exception:
            continue
    
    # Silent fail for known stale positions (don't spam logs)
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
    positions = _load_positions()
    open_positions = [p for p in positions if p.status == "open"]
    now = datetime.now(timezone.utc)

    # --- Risk checks ---
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        console.print(f"[yellow]  ⚠ Max positions ({MAX_OPEN_POSITIONS}) reached, skipping[/yellow]")
        return None

    # No duplicate market
    if any(p.market_id == market_id for p in open_positions):
        console.print(f"[yellow]  ⚠ Already have position on this market, skipping[/yellow]")
        return None

    # Correlated position check — DISABLED per Yu's request (don't limit indirect correlations)
    # Focus on ultra-short-term trades where correlation risk is minimal

    # Cooldown check
    closed_same = [p for p in positions if p.market_id == market_id and p.status == "closed" and p.exit_time]
    for cp in closed_same:
        exit_dt = datetime.fromisoformat(cp.exit_time)
        if now - exit_dt < timedelta(hours=COOLDOWN_HOURS):
            console.print(f"[yellow]  ⚠ Cooldown active for this market (exited {cp.exit_time})[/yellow]")
            return None

    # --- Half-Kelly position sizing ---
    kelly_cost = kelly_size(ai_probability, entry_price, confidence, bankroll)
    if kelly_cost < 1.0:
        console.print(f"[yellow]  ⚠ Kelly says no edge (ai={ai_probability:.1%} vs price={entry_price:.1%}, conf={confidence:.0%}) → $0[/yellow]")
        return None

    # Apply total exposure cap
    total_invested = sum(p.cost for p in open_positions)
    max_remaining = bankroll * MAX_TOTAL_EXPOSURE_PCT - total_invested
    allowed_cost = min(kelly_cost, max(0, max_remaining))

    if allowed_cost < 1.0:
        console.print(f"[yellow]  ⚠ Exposure limit reached (${total_invested:.0f} invested)[/yellow]")
        return None

    # Calculate shares
    shares = int(allowed_cost / entry_price) if entry_price > 0 else 0
    if shares < 3:
        console.print(f"[yellow]  ⚠ Position too small ({shares} shares, ${allowed_cost:.1f}), skipping[/yellow]")
        return None
    cost = round(shares * entry_price, 2)

    # Log Kelly calculation
    adj_p = 0.5 + (ai_probability - 0.5) * confidence
    console.print(f"[dim]  📐 Kelly: p={adj_p:.1%} c={entry_price:.1%} → half-kelly=${kelly_cost:.0f}, actual=${cost:.0f}[/dim]")

    # --- Adaptive exit strategy ---
    # Take profit: ratio scales with confidence
    if confidence >= 0.75:
        tp_ratio = HIGH_CONF_TP_RATIO  # 85% — confident, let it run
    elif confidence <= 0.55:
        tp_ratio = LOW_CONF_TP_RATIO   # 55% — less sure, grab early
    else:
        tp_ratio = BASE_TP_RATIO       # 70% — default

    target_price = round(entry_price + (ai_probability - entry_price) * tp_ratio, 4)

    # Stop loss: scales inversely with position size (small pos = wider stop)
    kelly_pct = cost / bankroll
    if kelly_pct <= 0.03:
        sl_ratio = WIDE_SL_RATIO       # 35% loss OK for tiny positions
    elif kelly_pct >= 0.10:
        sl_ratio = TIGHT_SL_RATIO      # 18% loss for big positions
    else:
        sl_ratio = BASE_SL_RATIO       # 25% default

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

    positions.append(pos)
    _save_positions(positions)
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
    else:  # BUY_NO
        pos.pnl = round((pos.entry_price - exit_price) * pos.shares, 2)

    _append_history(pos)
    _log_calibration(pos)
    icon = "🟢" if (pos.pnl or 0) >= 0 else "🔴"
    console.print(
        f"  {icon} CLOSED [{reason}]: {pos.question[:40]} | "
        f"PnL: ${pos.pnl:+.2f} ({pos.entry_price:.1%}→{exit_price:.1%})"
    )


CALIBRATION_FILE = Path(__file__).parent / "calibration_log.json"

def _log_calibration(pos: Position):
    """Record AI prediction vs actual outcome for calibration analysis."""
    try:
        existing = json.loads(CALIBRATION_FILE.read_text()) if CALIBRATION_FILE.exists() else []
    except Exception:
        existing = []
    
    # Did our prediction direction work?
    won = (pos.pnl or 0) > 0
    
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_id": pos.market_id,
        "question": pos.question[:80],
        "direction": pos.direction,
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price,
        "target_price": pos.target_price,
        "stop_loss": pos.stop_loss,
        "confidence": pos.confidence,
        "pnl": pos.pnl,
        "pnl_pct": round((pos.exit_price - pos.entry_price) / pos.entry_price * 100, 2) if pos.entry_price else 0,
        "exit_reason": pos.exit_reason,
        "won": won,
        "trigger_news": pos.trigger_news[:100],
        "hold_hours": round((datetime.fromisoformat(pos.exit_time) - datetime.fromisoformat(pos.entry_time)).total_seconds() / 3600, 1) if pos.exit_time and pos.entry_time else 0,
    }
    existing.append(entry)
    existing = existing[-500:]  # Keep last 500
    CALIBRATION_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


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

        # Current price depends on direction
        if pos.direction == "BUY_YES":
            current = yes_price
        else:
            current = 1 - yes_price

        # Update peak price (for trailing stop)
        peak = pos.peak_price or pos.entry_price
        if current > peak:
            pos.peak_price = current
            peak = current

        # --- Exit checks ---

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

        # 3. Trailing stop — activates after price moves 50%+ toward target
        target_move = pos.target_price - pos.entry_price
        if target_move > 0:
            progress = (peak - pos.entry_price) / target_move
            if progress >= TRAILING_STOP_ACTIVATION:
                # Trail at 30% below peak
                trail_stop = peak * (1 - TRAILING_STOP_DISTANCE)
                # Trailing stop must be above original stop loss
                effective_trail = max(trail_stop, pos.stop_loss)
                if current <= effective_trail:
                    profit_locked = (current - pos.entry_price) * pos.shares
                    _close_position(pos, current, f"TRAILING_STOP(peak={peak:.1%},locked=${profit_locked:+.1f})")
                    closed_count += 1
                    continue

        # 4. Time-based stop tightening
        entry_dt = datetime.fromisoformat(pos.entry_time)
        age_hours = (now - entry_dt).total_seconds() / 3600

        # After 12h: tighten stop loss by 50% (halfway between current stop and entry)
        if age_hours > 12 and age_hours <= 24:
            tightened_stop = (pos.stop_loss + pos.entry_price) / 2
            if current <= tightened_stop:
                _close_position(pos, current, "TIME_TIGHTENED_STOP")
                closed_count += 1
                continue

        # 5. Timeout — 24h with no movement
        if age_hours > TIMEOUT_HOURS:
            move = abs(current - pos.entry_price)
            if move < TIMEOUT_MOVE_THRESHOLD:
                _close_position(pos, current, "TIMEOUT_FLAT")
                closed_count += 1
                continue
            else:
                # 24h+ but price moved: close anyway, position is stale
                _close_position(pos, current, "TIMEOUT_AGED")
                closed_count += 1
                continue

    _save_positions(positions)
    return closed_count


def get_summary(bankroll: float = 1000.0) -> dict:
    """Get portfolio summary stats."""
    positions = _load_positions()
    open_pos = [p for p in positions if p.status == "open"]
    closed_pos = [p for p in positions if p.status == "closed"]

    total_invested = sum(p.cost for p in open_pos)
    winners = [p for p in closed_pos if (p.pnl or 0) > 0]
    realized_pnl = sum(p.pnl or 0 for p in closed_pos)
    win_rate = len(winners) / len(closed_pos) if closed_pos else 0

    return {
        "open_count": len(open_pos),
        "max_positions": MAX_OPEN_POSITIONS,
        "total_invested": total_invested,
        "exposure_pct": total_invested / bankroll if bankroll > 0 else 0,
        "closed_count": len(closed_pos),
        "realized_pnl": realized_pnl,
        "win_rate": win_rate,
        "open_positions": open_pos,
        "closed_positions": closed_pos,
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
    closed_today = [p for p in summary["closed_positions"]
                    if p.exit_time and datetime.fromisoformat(p.exit_time).date() == today]
    today_pnl = sum(p.pnl or 0 for p in closed_today)
    today_winners = len([p for p in closed_today if (p.pnl or 0) > 0])
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

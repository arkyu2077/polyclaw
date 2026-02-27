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
MAX_OPEN_POSITIONS = 5
MAX_POSITION_PCT = 0.10      # 10% bankroll per position
MAX_TOTAL_EXPOSURE_PCT = 0.30  # 30% total
COOLDOWN_HOURS = 1
TIMEOUT_HOURS = 24
TIMEOUT_MOVE_THRESHOLD = 0.02  # <2% move = "no movement"


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
    """Fetch fresh price for a market from Gamma API."""
    try:
        resp = httpx.get(
            f"https://gamma-api.polymarket.com/markets?id={market_id}",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[0]
        prices = json.loads(data.get("outcomePrices", "[]"))
        if prices:
            return float(prices[0])  # YES price
    except Exception as e:
        console.print(f"[dim red]  ⚠ Price fetch failed for {market_id[:12]}...: {e}[/dim red]")
    return None


def open_position(
    market_id: str,
    question: str,
    direction: str,
    entry_price: float,
    ai_probability: float,
    bankroll: float,
    trigger_news: str = "",
) -> Position | None:
    """Open a new paper position if risk checks pass. Returns Position or None."""
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

    # Cooldown check
    closed_same = [p for p in positions if p.market_id == market_id and p.status == "closed" and p.exit_time]
    for cp in closed_same:
        exit_dt = datetime.fromisoformat(cp.exit_time)
        if now - exit_dt < timedelta(hours=COOLDOWN_HOURS):
            console.print(f"[yellow]  ⚠ Cooldown active for this market (exited {cp.exit_time})[/yellow]")
            return None

    # Max per position
    max_cost = bankroll * MAX_POSITION_PCT
    # Max total exposure
    total_invested = sum(p.cost for p in open_positions)
    max_remaining = bankroll * MAX_TOTAL_EXPOSURE_PCT - total_invested
    allowed_cost = min(max_cost, max(0, max_remaining))

    if allowed_cost < 1.0:
        console.print(f"[yellow]  ⚠ Exposure limit reached (${total_invested:.0f} invested)[/yellow]")
        return None

    # Calculate shares
    shares = int(allowed_cost / entry_price) if entry_price > 0 else 0
    if shares < 5:
        console.print(f"[yellow]  ⚠ Position too small ({shares} shares), skipping[/yellow]")
        return None
    cost = round(shares * entry_price, 2)

    # Target: 70% of expected move
    target_price = round(entry_price + (ai_probability - entry_price) * 0.7, 4)
    # Stop loss: 25% below entry
    stop_loss = round(entry_price * 0.75, 4)

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
    icon = "🟢" if (pos.pnl or 0) >= 0 else "🔴"
    console.print(
        f"  {icon} CLOSED [{reason}]: {pos.question[:40]} | "
        f"PnL: ${pos.pnl:+.2f} ({pos.entry_price:.1%}→{exit_price:.1%})"
    )


def check_exits() -> int:
    """Check all open positions for exit conditions. Returns count of closed positions."""
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

        # Check exit conditions
        if current >= pos.target_price:
            _close_position(pos, current, "TAKE_PROFIT")
            closed_count += 1
        elif current <= pos.stop_loss:
            _close_position(pos, current, "STOP_LOSS")
            closed_count += 1
        else:
            # Timeout check
            entry_dt = datetime.fromisoformat(pos.entry_time)
            age = now - entry_dt
            if age > timedelta(hours=TIMEOUT_HOURS):
                move = abs(current - pos.entry_price)
                if move < TIMEOUT_MOVE_THRESHOLD:
                    _close_position(pos, current, "TIMEOUT")
                    closed_count += 1

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

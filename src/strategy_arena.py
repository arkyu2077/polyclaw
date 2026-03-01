"""Strategy Arena — run multiple strategy variants simultaneously on same signals."""

import json
import copy
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

from rich.console import Console
from rich.table import Table
from rich import box

from .config import get_config
from .db import get_positions, upsert_position, insert_trade, get_trades

console = Console()


@dataclass
class StrategyConfig:
    """A named strategy variant with tunable parameters."""
    name: str
    description: str

    # Core parameters
    kelly_fraction: float = 0.5
    ai_discount: float = 0.5
    min_edge: float = 0.02
    max_position_pct: float = 0.15

    # Exit parameters
    tp_ratio: float = 0.70
    sl_ratio: float = 0.75
    trailing_stop: bool = True
    trailing_activation: float = 0.5
    trailing_distance: float = 0.30
    timeout_hours: float = 24

    # Filters
    max_open_positions: int = 8
    min_confidence: float = 0.0
    correlated_limit_pct: float = 0.35


# Predefined strategy variants
STRATEGIES = {
    "baseline": StrategyConfig(
        name="baseline",
        description="当前策略（半Kelly、AI打半折、70%止盈）",
        kelly_fraction=0.5,
        ai_discount=0.5,
        tp_ratio=0.70,
        sl_ratio=0.75,
    ),
    "aggressive": StrategyConfig(
        name="aggressive",
        description="激进（全Kelly、AI打7折、85%止盈、宽止损）",
        kelly_fraction=0.75,
        ai_discount=0.7,
        min_edge=0.01,
        tp_ratio=0.85,
        sl_ratio=0.65,
        max_open_positions=12,
    ),
    "conservative": StrategyConfig(
        name="conservative",
        description="保守（四分之一Kelly、AI打3折、55%止盈、紧止损）",
        kelly_fraction=0.25,
        ai_discount=0.3,
        min_edge=0.04,
        tp_ratio=0.55,
        sl_ratio=0.85,
        min_confidence=0.55,
        max_open_positions=5,
    ),
    "sniper": StrategyConfig(
        name="sniper",
        description="狙击手（半Kelly、高门槛、超短期快进快出）",
        kelly_fraction=0.5,
        ai_discount=0.5,
        min_edge=0.06,
        tp_ratio=0.50,
        sl_ratio=0.82,
        trailing_stop=False,
        timeout_hours=6,
        min_confidence=0.60,
        max_open_positions=4,
    ),
    "trend_follower": StrategyConfig(
        name="trend_follower",
        description="趋势跟随（半Kelly、宽止盈、移动止损、长持仓）",
        kelly_fraction=0.5,
        ai_discount=0.5,
        min_edge=0.03,
        tp_ratio=0.90,
        sl_ratio=0.70,
        trailing_stop=True,
        trailing_activation=0.3,
        trailing_distance=0.20,
        timeout_hours=48,
        max_open_positions=10,
    ),
}


class StrategyRunner:
    """Runs a single strategy variant with its own positions."""

    def __init__(self, config: StrategyConfig, bankroll: float = 1000.0):
        self.config = config
        self.bankroll = bankroll

    def _load_positions(self) -> list[dict]:
        return get_positions(mode="arena", strategy=self.config.name)

    def _load_history(self) -> list[dict]:
        return get_trades(mode="arena", strategy=self.config.name)

    def kelly_size(self, ai_prob: float, entry_price: float, confidence: float) -> float:
        """Kelly with strategy-specific fraction and AI discount."""
        blend_factor = self.config.ai_discount * max(0.5, min(1.0, confidence))
        p = entry_price + (ai_prob - entry_price) * blend_factor

        p = max(0.01, min(0.99, p))
        q = 1 - p

        b = (1.0 / entry_price) - 1.0 if entry_price > 0.001 else 0
        if b <= 0:
            return 0.0

        f_star = (b * p - q) / b
        f_star = max(0, min(f_star, self.config.max_position_pct))
        f_star *= self.config.kelly_fraction

        return round(self.bankroll * f_star, 2)

    def try_open(self, market_id, question, direction, entry_price, ai_prob, confidence, trigger=""):
        """Try to open a position using this strategy's rules."""
        cfg = get_config()
        if entry_price < 0.03:
            return None
        positions = self._load_positions()
        open_pos = [p for p in positions if p.get("status") == "open"]

        if len(open_pos) >= self.config.max_open_positions:
            return None

        if any(p.get("market_id") == market_id for p in open_pos):
            return None

        # Check recently closed (avoid re-entry after stop-loss)
        history = self._load_history()
        recent_closes = [h for h in history
                         if h.get("market_id") == market_id
                         and h.get("exit_time")
                         and (datetime.now(timezone.utc) - datetime.fromisoformat(h["exit_time"])).total_seconds() < cfg.signal_cooldown_hours * 3600]
        if recent_closes:
            return None

        if confidence < self.config.min_confidence:
            return None

        # Correlated exposure check
        CORRELATION_KEYWORDS = {
            "btc": ["bitcoin", "btc"], "eth": ["ethereum", "eth"],
            "trump": ["trump", "truth social"],
        }
        q_lower = question.lower()
        for _, keywords in CORRELATION_KEYWORDS.items():
            if any(kw in q_lower for kw in keywords):
                corr_cost = sum(p.get("cost", 0) for p in open_pos if any(kw in (p.get("question") or "").lower() for kw in keywords))
                if corr_cost >= self.bankroll * self.config.correlated_limit_pct:
                    return None

        cost = self.kelly_size(ai_prob, entry_price, confidence)
        if cost < 1.0:
            return None

        total = sum(p.get("cost", 0) for p in open_pos)
        cost = min(cost, self.bankroll - total)
        if cost < 1.0:
            return None

        shares = int(cost / entry_price) if entry_price > 0 else 0
        if shares < 3:
            return None
        cost = round(shares * entry_price, 2)

        move = abs(ai_prob - entry_price)
        target = round(entry_price + move * self.config.tp_ratio, 4)
        stop = round(entry_price * self.config.sl_ratio, 4)

        pos_id = f"{market_id[:8]}_{datetime.now().strftime('%H%M%S')}"
        pos = {
            "id": pos_id,
            "trade_id": "",
            "mode": "arena",
            "strategy": self.config.name,
            "market_id": market_id,
            "token_id": "",
            "question": question,
            "direction": direction,
            "entry_price": entry_price,
            "shares": shares,
            "filled_shares": shares,
            "cost": cost,
            "target_price": target,
            "stop_loss": stop,
            "confidence": confidence,
            "status": "open",
            "order_id": "",
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "trigger_news": str(trigger)[:200],
            "neg_risk": 0,
            "peak_price": entry_price,
        }
        upsert_position(pos)
        return pos

    def check_exits(self, price_fetcher) -> int:
        """Check all open positions for exits. Returns count closed."""
        positions = self._load_positions()
        open_pos = [p for p in positions if p.get("status") == "open"]
        if not open_pos:
            return 0

        now = datetime.now(timezone.utc)
        closed = 0

        for pos in open_pos:
            price = price_fetcher(pos["market_id"])
            if price is None or price <= 0:
                continue

            current = price if pos["direction"] == "BUY_YES" else (1 - price)
            if current <= 0:
                continue

            peak = pos.get("peak_price") or pos["entry_price"]
            if current > peak:
                pos["peak_price"] = current
                peak = current

            reason = None

            if current >= pos["target_price"]:
                reason = "TAKE_PROFIT"
            elif current <= pos["stop_loss"]:
                reason = "STOP_LOSS"
            elif self.config.trailing_stop:
                target_move = pos["target_price"] - pos["entry_price"]
                if target_move > 0:
                    progress = (peak - pos["entry_price"]) / target_move
                    if progress >= self.config.trailing_activation:
                        trail_stop = peak * (1 - self.config.trailing_distance)
                        if current <= max(trail_stop, pos["stop_loss"]):
                            reason = "TRAILING_STOP"

            if not reason:
                age_h = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 3600
                if age_h > self.config.timeout_hours:
                    reason = "TIMEOUT"

            if reason:
                if pos["direction"] == "BUY_YES":
                    pnl = round((current - pos["entry_price"]) * pos["shares"], 2)
                else:
                    pnl = round((pos["entry_price"] - current) * pos["shares"], 2)
                pos["status"] = "closed"
                pos["exit_price"] = current
                pos["exit_time"] = now.isoformat()
                pos["exit_reason"] = reason
                pos["pnl"] = pnl
                upsert_position(pos)

                # Insert trade history
                try:
                    entry_dt = datetime.fromisoformat(pos["entry_time"])
                    hours = (now - entry_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    hours = 0

                insert_trade({
                    "position_id": pos["id"],
                    "mode": "arena",
                    "strategy": self.config.name,
                    "market_id": pos.get("market_id"),
                    "question": pos.get("question"),
                    "direction": pos.get("direction"),
                    "entry_price": pos.get("entry_price"),
                    "exit_price": current,
                    "shares": pos.get("shares"),
                    "cost": pos.get("cost"),
                    "pnl": pnl,
                    "fees": 0,
                    "entry_time": pos.get("entry_time"),
                    "exit_time": pos["exit_time"],
                    "exit_reason": reason,
                    "trigger_news": pos.get("trigger_news"),
                    "confidence": pos.get("confidence"),
                    "hold_hours": round(hours, 2),
                })
                closed += 1
            else:
                # Save peak price update
                upsert_position(pos)

        return closed

    def get_stats(self, price_fetcher=None) -> dict:
        """Get performance stats for this strategy."""
        positions = self._load_positions()
        history = self._load_history()
        open_pos = [p for p in positions if p.get("status") == "open"]

        realized = sum(h.get("pnl", 0) for h in history)
        unrealized = 0

        if price_fetcher:
            for p in open_pos:
                price = price_fetcher(p["market_id"])
                if price is not None:
                    current = price if p["direction"] == "BUY_YES" else (1 - price)
                    unrealized += (current - p["entry_price"]) * p["shares"]

        total_invested = sum(p.get("cost", 0) for p in open_pos)
        wins = sum(1 for h in history if h.get("pnl", 0) > 0)
        losses = sum(1 for h in history if h.get("pnl", 0) < 0)

        return {
            "strategy": self.config.name,
            "description": self.config.description,
            "open_count": len(open_pos),
            "closed_count": len(history),
            "invested": round(total_invested, 2),
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
            "roi_pct": round((realized + unrealized) / max(1, total_invested) * 100, 1) if total_invested > 0 else 0,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 1),
        }


def run_arena(estimates, bankroll: float = 1000.0, live_trading: bool = False):
    """Run all strategy variants against the same probability estimates.

    Each strategy applies its own AI discount and edge threshold,
    so aggressive strategies may open positions that conservative ones skip.
    If live_trading=True, sniper signals also place real CLOB orders.
    """
    cfg = get_config()
    active = set(cfg.active_strategies)

    # Build per-run copies so we never mutate global STRATEGIES
    run_configs = {}
    for name, base_config in STRATEGIES.items():
        if name not in active:
            continue
        cfg_copy = copy.deepcopy(base_config)
        overrides = cfg.strategy_overrides.get(name, {})
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if hasattr(cfg_copy, key):
                    setattr(cfg_copy, key, value)
        run_configs[name] = cfg_copy

    for name, config in run_configs.items():
        runner = StrategyRunner(config, bankroll)

        for est in estimates:
            market_price = est.current_price
            raw_ai = est.ai_probability

            yes_edge = raw_ai - market_price
            no_edge = market_price - raw_ai

            if yes_edge > no_edge and yes_edge >= config.min_edge:
                direction = "BUY_YES"
                entry_price = market_price
                ai_prob = raw_ai
            elif no_edge >= config.min_edge:
                direction = "BUY_NO"
                entry_price = 1 - market_price
                ai_prob = 1 - raw_ai
            else:
                continue

            if entry_price < 0.001 or entry_price > 0.999:
                continue

            edge_used = yes_edge if direction == "BUY_YES" else no_edge
            if entry_price < 0.01 or entry_price > 0.99:
                min_edge_for_extreme = max(config.min_edge, entry_price * 0.5)
                if edge_used < min_edge_for_extreme:
                    continue

            titles = est.signals.get("news_titles", [])
            trigger = est.signals.get("llm_reasoning", titles[0] if titles else "")

            paper_pos = runner.try_open(
                market_id=est.market_id,
                question=est.question,
                direction=direction,
                entry_price=entry_price,
                ai_prob=ai_prob,
                confidence=est.confidence,
                trigger=str(trigger)[:100],
            )

            # Live trading: if baseline opened a paper position, also place real order
            if live_trading and name == "baseline" and paper_pos is not None:
                try:
                    from .live_trader import open_live_position, get_balance, release_funds_for_signal
                    clob_ids = getattr(est, 'clob_token_ids', None) or []
                    if len(clob_ids) >= 2:
                        token_id = clob_ids[0] if direction == "BUY_YES" else clob_ids[1]
                        real_balance = get_balance()
                        scale = real_balance / bankroll
                        real_cost = min(paper_pos["cost"] * scale, cfg.max_order_size)
                        if real_cost >= 1.0 and real_balance < real_cost:
                            real_balance = release_funds_for_signal(real_cost)
                        if real_cost >= 1.0:
                            open_live_position(
                                market_id=est.market_id,
                                token_id=token_id,
                                question=est.question,
                                direction=direction,
                                price=entry_price,
                                size_usd=real_cost,
                                trigger_news=str(trigger)[:100],
                                target_price=paper_pos["target_price"],
                                stop_loss=paper_pos["stop_loss"],
                                ai_probability=getattr(est, 'ai_estimate', 0),
                                edge=paper_pos.get("edge", 0),
                                confidence=getattr(est, 'confidence', 0),
                                source=getattr(est, 'source', 'scanner'),
                                reasoning=getattr(est, 'reasoning', ''),
                            )
                            console.print(f"[bold cyan]  💰 LIVE ORDER placed: ${real_cost:.2f}[/bold cyan]")
                except Exception as e:
                    console.print(f"[red]  ❌ Live order failed: {e}[/red]")


def check_arena_exits(price_fetcher, bankroll: float = 1000.0):
    """Check exits for all strategy variants."""
    total = 0
    for name, config in STRATEGIES.items():
        runner = StrategyRunner(config, bankroll)
        closed = runner.check_exits(price_fetcher)
        total += closed
    return total


def arena_leaderboard(price_fetcher=None) -> str:
    """Generate a formatted leaderboard of all strategies."""
    cfg = get_config()
    active = set(cfg.active_strategies)
    stats = []
    for name, config in STRATEGIES.items():
        if name not in active:
            continue
        runner = StrategyRunner(config)
        s = runner.get_stats(price_fetcher)
        stats.append(s)

    stats.sort(key=lambda s: s["total_pnl"], reverse=True)

    lines = ["📊 **策略竞技场排行榜**\n"]
    for i, s in enumerate(stats):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] if i < 5 else f"{i+1}."
        pnl_icon = "📈" if s["total_pnl"] > 0 else "📉"
        lines.append(
            f"{medal} **{s['strategy']}** — {s['description']}\n"
            f"   {pnl_icon} PnL: **${s['total_pnl']:+.1f}** (ROI {s['roi_pct']:+.1f}%) | "
            f"持仓{s['open_count']}笔 投入${s['invested']:.0f} | "
            f"已平{s['closed_count']}笔 胜率{s['win_rate']:.0f}%"
        )

    return "\n".join(lines)

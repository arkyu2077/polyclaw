"""Edge detection with proper fee modeling and Kelly criterion sizing."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from .probability_engine import ProbEstimate
from .config import get_config
from .db import add_notification

FILTERED_LOG = Path(__file__).parent / "filtered_signals.json"
_MAX_FILTERED_ENTRIES = 500  # Keep last 500

# Human-readable filter reasons
_FILTER_LABELS = {
    "expiring_<1h": "Expiring in <1h",
    "edge_below_threshold": "Edge below threshold",
    "lottery_ticket": "Lottery ticket (<3c)",
    "price_too_high": "Price too high (>99.9%)",
    "absurd_edge": "Absurd edge (>40%)",
    "extreme_low_price_mismatch": "Low-price mismatch",
    "extreme_high_price_mismatch": "High-price mismatch",
    "too_few_shares": "Too few shares",
}


def _log_filtered(estimate: ProbEstimate, reason: str, details: dict = None):
    """Log a signal that was discovered but filtered out."""
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "market_id": estimate.market_id,
        "question": estimate.question[:80],
        "ai_probability": round(estimate.ai_probability, 4),
        "market_price": round(estimate.current_price, 4),
        "confidence": round(estimate.confidence, 4),
        "source": estimate.signals.get("source", ""),
        "reason": reason,
        **(details or {}),
    }
    try:
        entries = json.loads(FILTERED_LOG.read_text()) if FILTERED_LOG.exists() else []
    except (json.JSONDecodeError, OSError):
        entries = []
    entries.append(entry)
    entries = entries[-_MAX_FILTERED_ENTRIES:]
    FILTERED_LOG.write_text(json.dumps(entries, indent=2, ensure_ascii=False))

    # Write to notifications for OpenClaw consumption
    label = _FILTER_LABELS.get(reason, reason)
    edge_str = ""
    if details:
        yes_e = details.get("yes_edge")
        no_e = details.get("no_edge")
        if yes_e is not None:
            edge_str = f" | yes={yes_e:+.1%} no={no_e:+.1%}"
        edge_val = details.get("edge")
        if edge_val is not None:
            edge_str = f" | edge={edge_val:+.1%}"
    try:
        add_notification(
            f"[Filtered] {estimate.question[:60]} | "
            f"AI={estimate.ai_probability:.0%} vs Mkt={estimate.current_price:.0%} | "
            f"{label}{edge_str}",
            "SIGNAL_FILTERED",
        )
    except Exception:
        pass


def estimate_fee(price: float, market_type: str = "default") -> float:
    """
    Estimate Polymarket taker fee based on price and market type.
    
    Most markets are FEE-FREE (politics, crypto daily, stocks, weather, etc.)
    Only specific types charge taker fees:
    - NCAAB college basketball: max ~0.44% at 50¢
    - Serie A football: max ~0.44% at 50¢  
    - 5/15-min crypto (discontinued Dec 2025): max ~1.56% at 50¢
    
    We only model spread cost for free markets (~0.3-0.5%).
    """
    if market_type in ("ncaab", "serie_a", "sports_fee"):
        # Sports fee: rate=0.0175, exponent=1 → fee = 0.0175 * p * (1-p)
        fee = 0.0175 * price * (1 - price)
    else:
        fee = 0  # Most markets: zero taker fee

    # Spread cost: wider near 50¢, tighter at extremes
    spread_cost = 0.003 * 2 * price * (1 - price)  # ~0.3% max
    return fee + spread_cost


DEFAULT_BANKROLL = 1000.0


@dataclass
class TradeSignal:
    market_id: str
    question: str
    direction: str
    current_price: float
    ai_probability: float
    raw_edge: float
    fee_estimate: float
    edge: float  # after fees
    confidence: float
    reliability: str  # "high", "medium", "low"
    kelly_fraction: float
    position_size: float
    expected_shares: int
    expected_value: float
    signals: dict


def hours_to_expiry(end_date_str: str | None) -> float | None:
    """Compute hours until market expiry. Returns None if unknown."""
    if not end_date_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end_dt - now).total_seconds() / 3600
        return max(0, delta)
    except Exception:
        return None


def calculate_edge(estimate: ProbEstimate, bankroll: float = DEFAULT_BANKROLL,
                   min_edge: float | None = None) -> TradeSignal | None:
    cfg = get_config()
    if min_edge is None:
        min_edge = cfg.min_edge_threshold
    ai_prob = estimate.ai_probability
    market_price = estimate.current_price

    # Expiration awareness
    end_date = estimate.signals.get("end_date")
    hours_left = hours_to_expiry(end_date)
    
    if hours_left is not None:
        # Don't enter markets expiring in < 1 hour (not enough time to react)
        if hours_left < 1:
            _log_filtered(estimate, "expiring_<1h", {"hours_left": round(hours_left, 1)})
            return None
        
        # Markets expiring in < 6h: require higher edge (prices converge fast)
        if hours_left < 6:
            min_edge = max(min_edge, 0.05)  # Need 5%+ edge for short-term
        
        # Markets expiring in > 30 days: reduce confidence (too much uncertainty)
        if hours_left > 720:  # 30 days
            ai_prob = market_price + (ai_prob - market_price) * 0.7  # Shrink edge by 30%

    # Compute raw edges
    yes_raw_edge = ai_prob - market_price
    no_raw_edge = market_price - ai_prob

    # Fees for each side
    yes_fee = estimate_fee(market_price)
    no_fee = estimate_fee(1 - market_price)

    yes_edge = yes_raw_edge - yes_fee
    no_edge = no_raw_edge - no_fee

    if yes_edge > no_edge and yes_edge >= min_edge:
        direction = "BUY_YES"
        raw_edge = yes_raw_edge
        fee = yes_fee
        edge = yes_edge
        entry_price = market_price
    elif no_edge >= min_edge:
        direction = "BUY_NO"
        raw_edge = no_raw_edge
        fee = no_fee
        edge = no_edge
        entry_price = 1 - market_price
    else:
        _log_filtered(estimate, "edge_below_threshold", {
            "yes_edge": round(yes_edge, 4), "no_edge": round(no_edge, 4), "min_edge": round(min_edge, 4)
        })
        return None

    # Skip lottery tickets — prices below 3¢ are almost always correctly priced
    if entry_price < 0.03:
        _log_filtered(estimate, "lottery_ticket", {"entry_price": round(entry_price, 4), "direction": direction})
        return None

    if entry_price >= 0.999:
        _log_filtered(estimate, "price_too_high", {"entry_price": round(entry_price, 4), "direction": direction})
        return None

    # Sanity filter: reject absurdly large edges (likely AI hallucination)
    if edge > 0.40:  # >40% edge is almost certainly wrong
        _log_filtered(estimate, "absurd_edge", {"edge": round(edge, 4), "direction": direction})
        return None
    
    # Sanity filter: reject signals on extreme-priced markets with huge edge claims
    # e.g., market at 5% → AI says 45% (probably wrong)
    if market_price < 0.10 and ai_prob > 0.35:
        _log_filtered(estimate, "extreme_low_price_mismatch", {"market_price": round(market_price, 4), "ai_prob": round(ai_prob, 4)})
        return None
    if market_price > 0.90 and ai_prob < 0.65:
        _log_filtered(estimate, "extreme_high_price_mismatch", {"market_price": round(market_price, 4), "ai_prob": round(ai_prob, 4)})
        return None

    # Kelly criterion
    b = (1.0 / entry_price) - 1.0
    p = ai_prob if direction == "BUY_YES" else (1 - ai_prob)
    q = 1 - p
    kelly = (b * p - q) / b if b > 0 else 0
    kelly = max(0, min(kelly, cfg.max_kelly_fraction))
    kelly *= estimate.confidence

    position_size = round(bankroll * kelly, 2)
    expected_shares = max(0, int(position_size / entry_price)) if position_size > 0 else 0
    ev = round(edge * expected_shares * entry_price, 2)

    if expected_shares < cfg.min_shares:
        _log_filtered(estimate, "too_few_shares", {"expected_shares": expected_shares, "kelly": round(kelly, 4), "direction": direction})
        return None

    # Reliability indicator
    n_signals = estimate.signals.get("n_signals", 1)
    avg_imp = estimate.signals.get("avg_importance", 2)
    if n_signals >= 3 and avg_imp >= 3.5 and estimate.confidence > 0.5:
        reliability = "high"
    elif n_signals >= 2 and avg_imp >= 2.5:
        reliability = "medium"
    else:
        reliability = "low"

    return TradeSignal(
        market_id=estimate.market_id,
        question=estimate.question,
        direction=direction,
        current_price=estimate.current_price,
        ai_probability=estimate.ai_probability,
        raw_edge=round(raw_edge, 4),
        fee_estimate=round(fee, 4),
        edge=round(edge, 4),
        confidence=estimate.confidence,
        reliability=reliability,
        kelly_fraction=round(kelly, 4),
        position_size=position_size,
        expected_shares=expected_shares,
        expected_value=ev,
        signals=estimate.signals,
    )


def find_edges(estimates: list[ProbEstimate], bankroll: float = DEFAULT_BANKROLL,
               min_edge: float | None = None) -> list[TradeSignal]:
    signals = []
    for est in estimates:
        sig = calculate_edge(est, bankroll, min_edge)
        if sig:
            signals.append(sig)
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals

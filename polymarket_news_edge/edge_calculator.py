"""Edge detection with proper fee modeling and Kelly criterion sizing."""

from dataclasses import dataclass
from .probability_engine import ProbEstimate


def estimate_fee(price: float) -> float:
    """
    Estimate Polymarket taker fee based on price.
    Fee is higher at extreme prices, ~1.6% at midpoint, up to ~3.7% at extremes.
    Based on: fee ≈ 2 * price * (1 - price) * base_rate + fixed_component
    Simplified model from observed fee schedules.
    """
    # Polymarket uses a fee schedule that's roughly:
    # ~1% for prices near 0.5, scaling up as you approach 0 or 1
    # Plus there's spread cost
    base_fee = 0.016  # 1.6% baseline
    # At extreme prices, effective cost is higher due to spread
    spread_cost = 0.005 + 0.02 * (1 - 4 * price * (1 - price))
    return base_fee + spread_cost


MIN_EDGE_THRESHOLD = 0.03  # 3% after fees — much more realistic
MAX_KELLY_FRACTION = 0.10
DEFAULT_BANKROLL = 1000.0
MIN_SHARES = 5


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


def calculate_edge(estimate: ProbEstimate, bankroll: float = DEFAULT_BANKROLL,
                   min_edge: float = MIN_EDGE_THRESHOLD) -> TradeSignal | None:
    ai_prob = estimate.ai_probability
    market_price = estimate.current_price

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
        return None

    if entry_price <= 0.01 or entry_price >= 0.99:
        return None

    # Kelly criterion
    b = (1.0 / entry_price) - 1.0
    p = ai_prob if direction == "BUY_YES" else (1 - ai_prob)
    q = 1 - p
    kelly = (b * p - q) / b if b > 0 else 0
    kelly = max(0, min(kelly, MAX_KELLY_FRACTION))
    kelly *= estimate.confidence

    position_size = round(bankroll * kelly, 2)
    expected_shares = max(0, int(position_size / entry_price)) if position_size > 0 else 0
    ev = round(edge * expected_shares * entry_price, 2)

    if expected_shares < MIN_SHARES:
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
               min_edge: float = MIN_EDGE_THRESHOLD) -> list[TradeSignal]:
    signals = []
    for est in estimates:
        sig = calculate_edge(est, bankroll, min_edge)
        if sig:
            signals.append(sig)
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals

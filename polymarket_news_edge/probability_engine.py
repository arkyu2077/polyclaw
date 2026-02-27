"""Multi-signal probability estimation with directional logic and aggregation."""

import math
from dataclasses import dataclass

# Source credibility weights (0-1 scale)
SOURCE_WEIGHTS = {
    "Reuters": 1.0, "AP": 1.0, "Bloomberg": 1.0,
    "CoinDesk": 0.6, "The Block": 0.6,
    "BlockBeats": 0.8, "PANews": 0.7,
    "CryptoNews": 0.3, "CoinGecko-Trending": 0.2, "Fear&Greed": 0.3,
}

# Importance → max probability shift
IMPORTANCE_SHIFT = {
    1: 0.02,  # Minor news
    2: 0.04,
    3: 0.07,
    4: 0.12,
    5: 0.18,  # Major breaking news (Fed decision, war, etc.)
}


@dataclass
class ProbEstimate:
    market_id: str
    question: str
    current_price: float
    ai_probability: float
    confidence: float
    signals: dict


def compute_directional_shift(
    sentiment: float,
    yes_means_up: bool | None,
    question_type: str,
) -> float:
    """
    Compute the directional shift for YES probability.

    - If yes_means_up=True: bullish news → higher YES, bearish → lower YES
    - If yes_means_up=False: bullish news → lower YES, bearish → higher YES
    - If yes_means_up=None: use sentiment directly (positive → higher YES)
    """
    if yes_means_up is True:
        return sentiment  # Positive sentiment → positive shift
    elif yes_means_up is False:
        return -sentiment  # Positive sentiment → negative shift (YES means down)
    else:
        return sentiment  # Default: positive = higher YES


def estimate_single_signal(
    sentiment: float,
    match_score: float,
    importance: int,
    source: str,
    is_breaking: bool,
    yes_means_up: bool | None,
    question_type: str,
    news_age_hours: float = 1.0,
) -> tuple[float, float]:
    """
    Compute a single signal's shift and weight.
    Returns (directional_shift, signal_weight).
    """
    # Direction
    direction = compute_directional_shift(sentiment, yes_means_up, question_type)

    # Magnitude based on importance
    max_shift = IMPORTANCE_SHIFT.get(importance, 0.04)
    if is_breaking:
        max_shift *= 1.5

    # Source credibility
    cred = SOURCE_WEIGHTS.get(source, 0.3)

    # Match quality
    match_quality = match_score / 100.0

    # Recency decay
    recency = max(0.1, 1.0 / (1.0 + news_age_hours / 4.0))

    # The shift this signal suggests
    shift = direction * max_shift * cred * match_quality * recency

    # Weight for aggregation
    weight = cred * match_quality * recency * (1.5 if is_breaking else 1.0)

    return shift, weight


def aggregate_signals(
    current_price: float,
    signal_data: list[dict],
    volume: float,
) -> tuple[float, float]:
    """
    Aggregate multiple news signals for a single market.
    Returns (estimated_probability, confidence).
    """
    if not signal_data:
        return current_price, 0.0

    total_shift = 0.0
    total_weight = 0.0

    for sig in signal_data:
        shift, weight = estimate_single_signal(
            sentiment=sig["sentiment"],
            match_score=sig["match_score"],
            importance=sig["importance"],
            source=sig["source"],
            is_breaking=sig["is_breaking"],
            yes_means_up=sig.get("yes_means_up"),
            question_type=sig.get("question_type", "binary_event"),
        )
        total_shift += shift * weight
        total_weight += weight

    if total_weight == 0:
        return current_price, 0.0

    # Weighted average shift
    net_shift = total_shift / total_weight

    # Volume dampening: high volume markets are harder to move
    if volume > 1_000_000:
        net_shift *= 0.4
    elif volume > 100_000:
        net_shift *= 0.65

    # Apply shift
    estimated = current_price + net_shift
    estimated = max(0.02, min(0.98, estimated))

    # Confidence based on signal count, quality, and agreement
    n_signals = len(signal_data)
    signal_agreement = abs(net_shift) / (sum(abs(s) for s, _ in [
        estimate_single_signal(
            sig["sentiment"], sig["match_score"], sig["importance"],
            sig["source"], sig["is_breaking"], sig.get("yes_means_up"),
            sig.get("question_type", "binary_event"),
        ) for sig in signal_data
    ]) / max(1, n_signals) + 0.001) if n_signals > 0 else 0

    avg_match = sum(s["match_score"] for s in signal_data) / n_signals
    avg_cred = sum(SOURCE_WEIGHTS.get(s["source"], 0.3) for s in signal_data) / n_signals

    confidence = min(0.90, (
        (avg_match / 100.0) * 0.3 +
        avg_cred * 0.3 +
        min(1.0, n_signals / 3.0) * 0.2 +
        min(1.0, signal_agreement) * 0.2
    ))

    return round(estimated, 4), round(confidence, 4)


def compute_estimates(signals, markets_by_id: dict) -> list[ProbEstimate]:
    """Aggregate ALL news signals per market, then compute probability."""
    # Group signals by market
    market_signals: dict[str, dict] = {}  # market_id -> {market_info, signal_data: [...]}

    for signal in signals:
        for match in signal.matched_markets:
            mid = match["market_id"]
            if mid not in market_signals:
                prices = match.get("outcomePrices", [])
                market_signals[mid] = {
                    "question": match["question"],
                    "current_yes": prices[0] if prices else 0.5,
                    "volume": match.get("volume", 0),
                    "market_meta": match.get("market_meta", {}),
                    "signal_data": [],
                    "news_titles": [],
                }

            market_signals[mid]["signal_data"].append({
                "sentiment": signal.sentiment,
                "match_score": match["match_score"],
                "importance": signal.importance,
                "source": signal.news_source,
                "is_breaking": signal.is_breaking,
                "yes_means_up": match.get("market_meta", {}).get("yes_means_up"),
                "question_type": match.get("market_meta", {}).get("question_type", "binary_event"),
            })
            market_signals[mid]["news_titles"].append(signal.news_title)

    estimates = []
    for mid, data in market_signals.items():
        prob, conf = aggregate_signals(
            current_price=data["current_yes"],
            signal_data=data["signal_data"],
            volume=data["volume"],
        )

        estimates.append(ProbEstimate(
            market_id=mid,
            question=data["question"],
            current_price=data["current_yes"],
            ai_probability=prob,
            confidence=conf,
            signals={
                "n_signals": len(data["signal_data"]),
                "news_titles": data["news_titles"],
                "avg_importance": round(sum(s["importance"] for s in data["signal_data"]) / len(data["signal_data"]), 1),
                "market_meta": data["market_meta"],
            },
        ))

    return estimates


def merge_llm_estimates(
    keyword_estimates: list[ProbEstimate],
    llm_signals: list[dict],
) -> list[ProbEstimate]:
    """Merge LLM signals into keyword estimates. LLM takes priority on conflicts."""
    estimates_by_id = {e.market_id: e for e in keyword_estimates}

    for sig in llm_signals:
        mid = sig["market_id"]
        current_yes = float(sig.get("current_yes", 0.5))
        if mid in estimates_by_id:
            # LLM overrides probability estimate (more accurate)
            est = estimates_by_id[mid]
            est.ai_probability = sig["estimated_probability"]
            est.confidence = max(est.confidence, sig["confidence"])
            est.signals["llm_reasoning"] = sig.get("reasoning", "")
            est.signals["source"] = "LLM+keywords"
            if sig["news_title"] not in est.signals.get("news_titles", []):
                est.signals.setdefault("news_titles", []).append(sig["news_title"])
        else:
            # New market from LLM only
            estimates_by_id[mid] = ProbEstimate(
                market_id=mid,
                question=sig["question"],
                current_price=current_yes,
                ai_probability=sig["estimated_probability"],
                confidence=sig["confidence"],
                signals={
                    "n_signals": 1,
                    "news_titles": [sig["news_title"]],
                    "avg_importance": 4,
                    "llm_reasoning": sig.get("reasoning", ""),
                    "source": "LLM",
                },
            )

    return list(estimates_by_id.values())

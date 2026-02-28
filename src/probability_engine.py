"""Multi-signal probability estimation with directional logic, freshness decay, and source-adaptive confidence."""

import math
from datetime import datetime, timezone
from dataclasses import dataclass

# Source credibility weights (0-1 scale)
SOURCE_WEIGHTS = {
    # Tier 1: Wire services / official data — highest credibility
    "Reuters": 1.0, "AP": 1.0, "Bloomberg": 1.0,
    # Tier 2: Crypto-native quality media
    "CoinDesk": 0.6, "The Block": 0.6,
    "BlockBeats": 0.8, "PANews": 0.7,
    # Tier 3: Aggregated / sentiment
    "CryptoNews": 0.3, "CoinGecko-Trending": 0.2, "Fear&Greed": 0.3,
    # Tier 4: On-chain data (factual, high signal)
    "BTC-Whale": 0.85, "ETH-Gas": 0.7, "BTC-Fees": 0.6,
    "DeFi-TVL": 0.8, "Exchange-Flow": 0.9,
    # Tier 5: Sports (ESPN is reliable for sports facts)
    "ESPN": 0.85, "ESPN-NBA": 0.85, "ESPN-NFL": 0.85,
    "ESPN-Soccer": 0.80, "ESPN-MLB": 0.80,
    # Tier 6: Price anomaly (factual — something IS happening)
    "Price-Alert": 0.95, "Volume-Spike": 0.80,
}

# Source → base confidence for LLM signals (adaptive by source type)
SOURCE_CONFIDENCE = {
    # Official / data releases → very high confidence in the fact itself
    "Reuters": 0.90, "AP": 0.90, "Bloomberg": 0.85,
    # On-chain data → factual, but interpretation varies
    "BTC-Whale": 0.80, "Exchange-Flow": 0.85, "DeFi-TVL": 0.75,
    "BTC-Fees": 0.65, "ETH-Gas": 0.65,
    # Sports
    "ESPN": 0.80, "ESPN-NBA": 0.80, "ESPN-NFL": 0.80,
    "ESPN-Soccer": 0.75, "ESPN-MLB": 0.75,
    # Price anomaly — high confidence in the FACT, interpretation varies
    "Price-Alert": 0.90, "Volume-Spike": 0.75,
    # Crypto media → decent but sometimes speculative
    "CoinDesk": 0.60, "The Block": 0.60, "BlockBeats": 0.65, "PANews": 0.60,
    # Aggregated / trending → low confidence
    "CryptoNews": 0.40, "CoinGecko-Trending": 0.30, "Fear&Greed": 0.35,
}

# Importance → max probability shift
IMPORTANCE_SHIFT = {
    1: 0.02,  # Minor news
    2: 0.04,
    3: 0.07,
    4: 0.12,
    5: 0.18,  # Major breaking news (Fed decision, war, etc.)
}

# Freshness half-life by news type (hours) — after this time, signal strength halves
FRESHNESS_HALFLIFE = {
    "data_release": 0.5,     # CPI/GDP/earnings → priced in within 30 min
    "official_statement": 2.0,  # Fed/White House → 2 hours
    "breaking_news": 1.0,    # Breaking → 1 hour
    "analysis": 6.0,         # Analysis pieces → 6 hours
    "trending": 12.0,        # Trending/sentiment → 12 hours
    "onchain": 2.0,          # On-chain events → 2 hours
    "default": 4.0,          # Default → 4 hours
}


def classify_news_type(source: str, title: str, is_breaking: bool) -> str:
    """Classify news type for freshness decay calculation."""
    title_lower = title.lower()

    # Price anomalies — extremely time-sensitive
    if source in ("Price-Alert", "Volume-Spike"):
        return "data_release"  # Fastest decay — price info is priced in quickly

    # Sports — injury news has short window, results are immediate
    if source.startswith("ESPN"):
        if any(w in title_lower for w in ["injury", "ruled out", "out for", "questionable"]):
            return "breaking_news"  # Injury = fast decay
        return "official_statement"  # Other sports news = moderate decay

    # On-chain sources
    if source in ("BTC-Whale", "Exchange-Flow", "DeFi-TVL", "BTC-Fees", "ETH-Gas"):
        return "onchain"

    # Data releases
    if any(w in title_lower for w in ["cpi", "gdp", "jobs report", "employment", "nonfarm",
                                        "earnings", "revenue", "quarterly", "q1 ", "q2 ", "q3 ", "q4 "]):
        return "data_release"

    # Official statements
    if any(w in title_lower for w in ["federal reserve", "fomc", "powell", "white house",
                                        "sec ", "official", "announces", "signed", "executive order"]):
        return "official_statement"

    if is_breaking:
        return "breaking_news"

    # Trending / sentiment
    if source in ("CoinGecko-Trending", "Fear&Greed", "CryptoNews"):
        return "trending"

    return "default"


def compute_freshness(published_str: str, news_type: str = "default") -> float:
    """
    Compute freshness multiplier (0.0 - 1.0) based on news age and type.
    Uses exponential decay with type-specific half-life.
    
    Fresh news (< half-life) → 0.5 - 1.0
    Old news (> 2x half-life) → < 0.25
    """
    try:
        if not published_str:
            return 0.5  # Unknown age → assume moderate

        pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_hours = max(0, (now - pub_dt).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.5

    halflife = FRESHNESS_HALFLIFE.get(news_type, FRESHNESS_HALFLIFE["default"])

    # Exponential decay: freshness = 2^(-age/halflife)
    freshness = math.pow(2, -age_hours / halflife)

    return max(0.05, min(1.0, freshness))  # Floor at 5%, cap at 100%


def get_source_confidence(source: str) -> float:
    """Get base confidence level for a news source."""
    return SOURCE_CONFIDENCE.get(source, 0.50)


@dataclass
class ProbEstimate:
    market_id: str
    question: str
    current_price: float
    ai_probability: float
    confidence: float
    signals: dict
    clob_token_ids: list = None  # [yes_token_id, no_token_id]
    neg_risk: bool = False


def compute_directional_shift(
    sentiment: float,
    yes_means_up: bool | None,
    question_type: str,
) -> float:
    """
    Compute the directional shift for YES probability.

    - If yes_means_up=True: bullish news → higher YES, bearish → lower YES
    - If yes_means_up=False: bullish news → lower YES, bearish → higher YES
    - If yes_means_up=None: we DON'T KNOW the relationship → return 0
      (prevents blind bullish bias on unrelated markets)
    """
    if yes_means_up is True:
        return sentiment  # Positive sentiment → positive shift
    elif yes_means_up is False:
        return -sentiment  # Positive sentiment → negative shift (YES means down)
    else:
        # Ambiguous: attenuate by 70% instead of zeroing out
        # This still generates signals but with much less conviction
        return sentiment * 0.3


def estimate_single_signal(
    sentiment: float,
    match_score: float,
    importance: int,
    source: str,
    is_breaking: bool,
    yes_means_up: bool | None,
    question_type: str,
    news_age_hours: float = 1.0,
    published_str: str = "",
    news_title: str = "",
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

    # Freshness decay (replaces old linear recency)
    news_type = classify_news_type(source, news_title, is_breaking)
    if published_str:
        freshness = compute_freshness(published_str, news_type)
    else:
        # Fallback to old method if no publish time
        freshness = max(0.1, 1.0 / (1.0 + news_age_hours / 4.0))

    # The shift this signal suggests
    shift = direction * max_shift * cred * match_quality * freshness

    # Weight for aggregation — freshness is now a major factor
    weight = cred * match_quality * freshness * (1.5 if is_breaking else 1.0)

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
            published_str=sig.get("published", ""),
            news_title=sig.get("news_title", ""),
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

    # Confidence calculation — source-adaptive + freshness-weighted
    n_signals = len(signal_data)

    # Average source confidence (adaptive per source type)
    avg_source_conf = sum(get_source_confidence(s["source"]) for s in signal_data) / n_signals

    # Average match quality
    avg_match = sum(s["match_score"] for s in signal_data) / n_signals

    # Freshness factor: best (freshest) signal's freshness
    freshness_scores = []
    for sig in signal_data:
        news_type = classify_news_type(sig["source"], sig.get("news_title", ""), sig["is_breaking"])
        f = compute_freshness(sig.get("published", ""), news_type)
        freshness_scores.append(f)
    best_freshness = max(freshness_scores) if freshness_scores else 0.5

    # Signal count bonus (more independent sources = higher confidence)
    unique_sources = len(set(s["source"] for s in signal_data))
    source_diversity = min(1.0, unique_sources / 3.0)

    confidence = min(0.92, (
        avg_source_conf * 0.30 +         # Source credibility
        (avg_match / 100.0) * 0.20 +     # Match quality
        best_freshness * 0.25 +           # Freshness (most important upgrade)
        source_diversity * 0.15 +         # Multiple independent sources
        min(1.0, n_signals / 3.0) * 0.10  # Signal count
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
                    "end_date": match.get("endDate", ""),
                    "clob_token_ids": match.get("clobTokenIds", []),
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
                "published": getattr(signal, "published", ""),
                "news_title": signal.news_title,
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
                "end_date": data.get("end_date", ""),
            },
            clob_token_ids=data.get("clob_token_ids", []),
        ))

    return estimates


from .config import get_config


def discount_ai_probability(ai_prob: float, market_price: float) -> float:
    """
    Discount AI probability estimate toward market price.

    AI says 80%, market says 50% → discounted = 50% + (80%-50%) * 0.5 = 65%

    Rationale: the market aggregates thousands of participants' info.
    AI analysis adds value but shouldn't override the market entirely.
    """
    cfg = get_config()
    move = ai_prob - market_price
    discounted = market_price + move * cfg.ai_estimate_discount
    return max(0.02, min(0.98, round(discounted, 4)))


def merge_llm_estimates(
    keyword_estimates: list[ProbEstimate],
    llm_signals: list[dict],
) -> list[ProbEstimate]:
    """Merge LLM signals into keyword estimates. LLM takes priority on conflicts.
    AI estimates are discounted by 50% toward market price."""
    estimates_by_id = {e.market_id: e for e in keyword_estimates}

    for sig in llm_signals:
        mid = sig["market_id"]
        current_yes = float(sig.get("current_yes", 0.5))
        if mid in estimates_by_id:
            # LLM overrides but with discount
            est = estimates_by_id[mid]
            raw_ai = sig["estimated_probability"]
            est.ai_probability = discount_ai_probability(raw_ai, est.current_price)
            est.confidence = max(est.confidence, sig["confidence"])
            est.signals["raw_ai_probability"] = raw_ai  # Keep original for reference
            est.signals["llm_reasoning"] = sig.get("reasoning", "")
            est.signals["source"] = "LLM+keywords"
            if sig["news_title"] not in est.signals.get("news_titles", []):
                est.signals.setdefault("news_titles", []).append(sig["news_title"])
        else:
            # New market from LLM only — apply discount
            raw_ai = sig["estimated_probability"]
            discounted = discount_ai_probability(raw_ai, current_yes)
            estimates_by_id[mid] = ProbEstimate(
                market_id=mid,
                question=sig["question"],
                current_price=current_yes,
                ai_probability=discounted,
                confidence=sig["confidence"],
                clob_token_ids=sig.get("clob_token_ids", []),
                signals={
                    "n_signals": 1,
                    "news_titles": [sig["news_title"]],
                    "avg_importance": 4,
                    "llm_reasoning": sig.get("reasoning", ""),
                    "source": "LLM",
                    "raw_ai_probability": raw_ai,
                },
            )

    return list(estimates_by_id.values())

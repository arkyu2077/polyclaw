"""News-to-market matching with category tagging, entity matching, and question parsing."""

import re
import hashlib
from dataclasses import dataclass
from rapidfuzz import fuzz

# ── Categories ──────────────────────────────────────────────────────────────

CATEGORIES = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple",
        "dogecoin", "doge", "crypto", "blockchain", "defi", "nft", "stablecoin",
        "binance", "coinbase", "token", "altcoin", "memecoin", "halving",
    ],
    "politics": [
        "trump", "biden", "desantis", "rfk", "kennedy", "haley", "election",
        "vote", "democrat", "republican", "gop", "congress", "senate",
        "impeach", "campaign", "poll", "primary", "nominee", "candidate",
        "governor", "president", "white house", "maga",
    ],
    "sports": [
        "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "hockey", "super bowl", "world cup", "champion", "playoff",
        "tournament", "match", "team", "coach", "player", "league", "espn",
        "athlete", "ufc", "boxing", "tennis", "f1", "formula 1", "grand prix",
    ],
    "economics": [
        "federal reserve", "fed ", "fomc", "interest rate", "rate cut", "rate hike",
        "powell", "inflation", "cpi", "gdp", "recession", "employment", "jobs",
        "unemployment", "treasury", "bond", "yield", "tariff", "trade war",
        "debt ceiling", "fiscal", "monetary", "economic",
    ],
    "tech": [
        "artificial intelligence", " ai ", "openai", "chatgpt", "llm", "google",
        "apple", "microsoft", "meta", "nvidia", "semiconductor", "chip",
        "software", "startup", "ipo", "tech",
    ],
    "geopolitics": [
        "war", "conflict", "invasion", "military", "nato", "sanction",
        "china", "russia", "ukraine", "taiwan", "iran", "north korea",
        "missile", "nuclear", "diplomat", "ceasefire", "treaty",
    ],
}

# ── Entities with specificity ───────────────────────────────────────────────

ENTITIES = {
    # Crypto - specific
    "bitcoin":   {"keywords": ["bitcoin", "btc", "比特币"], "category": "crypto", "specific": True},
    "ethereum":  {"keywords": ["ethereum", "eth ", "以太坊"], "category": "crypto", "specific": True},
    "solana":    {"keywords": ["solana", "sol ", "索拉纳"], "category": "crypto", "specific": True},
    "xrp":       {"keywords": ["xrp", "ripple"], "category": "crypto", "specific": True},
    "dogecoin":  {"keywords": ["dogecoin", "doge"], "category": "crypto", "specific": True},
    "binance":   {"keywords": ["binance", "币安"], "category": "crypto", "specific": True},
    "whale":     {"keywords": ["whale", "巨鲸", "大户"], "category": "crypto", "specific": False},
    "airdrop":   {"keywords": ["airdrop", "空投"], "category": "crypto", "specific": False},
    "etf":       {"keywords": ["etf", "ETF"], "category": "economics", "specific": False},
    # Politics - specific
    "trump":     {"keywords": ["trump", "donald trump", "特朗普"], "category": "politics", "specific": True},
    "biden":     {"keywords": ["biden", "joe biden"], "category": "politics", "specific": True},
    "desantis":  {"keywords": ["desantis"], "category": "politics", "specific": True},
    "rfk":       {"keywords": ["rfk", "robert kennedy"], "category": "politics", "specific": True},
    "haley":     {"keywords": ["nikki haley", "haley"], "category": "politics", "specific": True},
    "elon musk": {"keywords": ["elon musk", "musk"], "category": "tech", "specific": True},
    # Institutions - less specific
    "fed":       {"keywords": ["federal reserve", "fomc", "powell", "rate cut", "rate hike", "美联储", "降息", "加息", "鲍威尔"], "category": "economics", "specific": False},
    "sec":       {"keywords": ["sec ", "securities and exchange", "gensler", "美国证监会"], "category": "economics", "specific": False},
    # Macro - not specific
    "inflation": {"keywords": ["inflation", "cpi", "consumer price", "通胀", "消费者价格"], "category": "economics", "specific": False},
    "recession": {"keywords": ["recession", "gdp contraction"], "category": "economics", "specific": False},
    "tariff":    {"keywords": ["tariff", "trade war"], "category": "economics", "specific": False},
    # Geopolitics
    "ukraine":   {"keywords": ["ukraine", "kyiv", "zelenskyy"], "category": "geopolitics", "specific": True},
    "russia":    {"keywords": ["russia", "putin", "kremlin"], "category": "geopolitics", "specific": True},
    "china":     {"keywords": ["china", "beijing", "xi jinping"], "category": "geopolitics", "specific": True},
    "war":       {"keywords": ["war", "战争", "军事", "冲突"], "category": "geopolitics", "specific": False},
}

# ── Market question type parsing ────────────────────────────────────────────

PRICE_THRESHOLD_PATTERNS = [
    r"(?:reach|hit|above|over|exceed|break|surpass)\s+\$?([\d,.]+[kmb]?)",
    r"(?:below|under|dip|fall|drop)\s+(?:to\s+)?\$?([\d,.]+[kmb]?)",
]

DIRECTION_BEARISH_MARKET = [  # markets where YES = price going DOWN
    "dip", "fall", "drop", "below", "under", "crash", "decline", "lose",
]
DIRECTION_BULLISH_MARKET = [  # markets where YES = price going UP
    "reach", "hit", "above", "over", "exceed", "break", "surpass", "rise",
]


@dataclass
class MarketMeta:
    """Parsed metadata about a market question."""
    category: str
    question_type: str  # "price_threshold", "binary_event", "yes_no"
    yes_means_up: bool | None  # True if YES = bullish outcome, None if unclear
    entities: list[str]


@dataclass
class NewsSignal:
    news_id: str
    news_title: str
    news_source: str
    entities: list[str]
    category: str
    sentiment: float
    importance: int  # 1-5
    is_breaking: bool
    matched_markets: list[dict]
    published: str = ""  # ISO timestamp for freshness decay


# ── Sentiment ───────────────────────────────────────────────────────────────

POSITIVE_WORDS = {
    "surge", "rise", "gain", "bull", "bullish", "up", "high", "approve", "pass",
    "win", "adopt", "launch", "boost", "rally", "record", "positive", "growth",
    "accept", "support", "success", "breakthrough", "agreement", "optimism",
    "soar", "spike", "upgrade", "strong", "beat", "outperform",
    # Chinese
    "上涨", "突破", "利好", "反弹", "新高", "看多", "牛市", "暴涨", "批准", "通过",
}
NEGATIVE_WORDS = {
    "crash", "fall", "drop", "bear", "bearish", "down", "low", "reject", "fail",
    "lose", "ban", "halt", "plunge", "decline", "negative", "recession",
    "collapse", "oppose", "block", "delay", "concern", "fear", "risk",
    "warning", "slump", "cut", "weak", "miss", "underperform", "crisis",
    "default", "panic", "selloff", "sell-off", "dump", "liquidat",
    # Chinese
    "下跌", "暴跌", "利空", "清算", "爆仓", "看空", "熊市", "崩盘", "拒绝", "禁止", "抛售",
}
NEGATION_WORDS = {"not", "no", "never", "neither", "nor", "barely", "hardly", "unlikely"}


def nuanced_sentiment(text: str) -> float:
    """Sentiment with negation handling and phrase awareness. Handles Chinese + English."""
    text_lower = text.lower()
    words = re.findall(r'\w+', text_lower)
    score = 0.0
    total = 0
    for i, w in enumerate(words):
        is_pos = w in POSITIVE_WORDS
        is_neg = w in NEGATIVE_WORDS
        if not (is_pos or is_neg):
            continue
        # Check for negation in preceding 3 words
        negated = any(words[max(0, i - 3):i].__contains__(n) for n in NEGATION_WORDS)
        if is_pos:
            score += -1.0 if negated else 1.0
        elif is_neg:
            score += 1.0 if negated else -1.0
        total += 1
    # Chinese sentiment: check as substrings (Chinese words aren't split by \w+)
    for w in POSITIVE_WORDS:
        if len(w) > 1 and ord(w[0]) > 127 and w in text:
            score += 1.0
            total += 1
    for w in NEGATIVE_WORDS:
        if len(w) > 1 and ord(w[0]) > 127 and w in text:
            score -= 1.0
            total += 1
    if total == 0:
        return 0.0
    return round(max(-1.0, min(1.0, score / total)), 3)


# ── Category detection ──────────────────────────────────────────────────────

def detect_category(text: str) -> str:
    """Detect primary category of text."""
    text_lower = text.lower()
    scores = {}
    for cat, keywords in CATEGORIES.items():
        scores[cat] = sum(1 for kw in keywords if kw in text_lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def detect_market_meta(market: dict) -> MarketMeta:
    """Parse market question to extract metadata."""
    q = market["question"].lower()
    desc = market.get("description", "").lower()
    full = f"{q} {desc}"

    category = detect_category(full)
    entities = extract_entities(full)

    # Determine question type and direction
    is_price = any(re.search(p, q) for p in PRICE_THRESHOLD_PATTERNS)
    if is_price:
        question_type = "price_threshold"
        yes_means_up = not any(w in q for w in DIRECTION_BEARISH_MARKET)
    else:
        question_type = "binary_event"
        # For binary events, try to infer direction from keywords
        bullish_count = sum(1 for w in DIRECTION_BULLISH_MARKET if w in q)
        bearish_count = sum(1 for w in DIRECTION_BEARISH_MARKET if w in q)
        if bullish_count > bearish_count:
            yes_means_up = True
        elif bearish_count > bullish_count:
            yes_means_up = False
        else:
            yes_means_up = None  # Ambiguous

    return MarketMeta(
        category=category,
        question_type=question_type,
        yes_means_up=yes_means_up,
        entities=entities,
    )


# ── Entity extraction ───────────────────────────────────────────────────────

def extract_entities(text: str) -> list[str]:
    """Extract recognized entities from text."""
    text_lower = text.lower()
    found = []
    for entity, info in ENTITIES.items():
        for kw in info["keywords"]:
            if kw in text_lower:
                found.append(entity)
                break
    return found


# ── Market matching ─────────────────────────────────────────────────────────

def match_markets(
    news_entities: list[str],
    news_category: str,
    markets: list[dict],
    threshold: int = 75,
) -> list[dict]:
    """Match news to markets with category filtering and entity requirements."""
    matches = []
    for market in markets:
        q = market["question"].lower()
        meta = detect_market_meta(market)

        # CATEGORY GATE: only match same category (or unknown)
        if news_category != "unknown" and meta.category != "unknown":
            if news_category != meta.category:
                continue

        # ENTITY MATCHING: require at least one specific entity overlap
        market_entities = set(meta.entities)
        news_entity_set = set(news_entities)
        shared = market_entities & news_entity_set
        if not shared:
            # Try fuzzy as fallback but require high threshold
            best_score = 0
            best_entity = ""
            for entity in news_entities:
                for kw in ENTITIES.get(entity, {}).get("keywords", [entity]):
                    if kw in q:
                        best_score = 90
                        best_entity = entity
                        break
                if best_score < 75:
                    score = fuzz.partial_ratio(entity, q)
                    if score > best_score:
                        best_score = score
                        best_entity = entity
            if best_score < threshold:
                continue
            has_specific = any(
                ENTITIES.get(e, {}).get("specific", False) for e in [best_entity]
            )
        else:
            best_score = 90
            best_entity = list(shared)[0]
            has_specific = any(
                ENTITIES.get(e, {}).get("specific", False) for e in shared
            )

        # Require at least one specific entity unless score is very high
        if not has_specific and best_score < 85:
            continue

        matches.append({
            "market_id": market["id"],
            "question": market["question"],
            "match_score": best_score,
            "matched_entity": best_entity,
            "outcomePrices": market["outcomePrices"],
            "volume": market["volume"],
            "endDate": market.get("endDate", ""),
            "clobTokenIds": market.get("clobTokenIds", []),
            "condition_id": market.get("condition_id", ""),
            "neg_risk": market.get("neg_risk", False),
            "market_meta": {
                "category": meta.category,
                "question_type": meta.question_type,
                "yes_means_up": meta.yes_means_up,
            },
        })

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches[:5]  # Tighter limit


# ── News importance & dedup ─────────────────────────────────────────────────

SOURCE_CREDIBILITY = {
    "Reuters": 5, "AP": 5, "Bloomberg": 5,
    "CoinDesk": 3, "The Block": 3, "BlockBeats": 3, "PANews": 3,
    "CryptoNews": 2, "CoinGecko-Trending": 2, "Fear&Greed": 2,
}

BREAKING_KEYWORDS = [
    "breaking", "just in", "urgent", "alert", "developing",
    "exclusive", "flash", "confirmed",
    "快讯", "突发", "刚刚",
]

IMPORTANCE_KEYWORDS = {
    5: ["fed ", "fomc", "rate decision", "rate cut", "rate hike", "war ", "invasion", "default"],
    4: ["regulation", "ban", "approve", "etf", "indictment", "sanction", "crash"],
    3: ["earnings", "report", "announce", "launch", "partnership"],
    2: ["trending", "rumor", "speculation", "opinion"],
}


def score_importance(title: str, source: str) -> int:
    """Score news importance 1-5."""
    title_lower = title.lower()
    base = SOURCE_CREDIBILITY.get(source, 1)
    keyword_score = 1
    for level, keywords in IMPORTANCE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            keyword_score = max(keyword_score, level)
    return min(5, max(1, (base + keyword_score) // 2))


def is_breaking(title: str) -> bool:
    title_lower = title.lower()
    return any(kw in title_lower for kw in BREAKING_KEYWORDS)


def deduplicate_news(news_items: list[dict], threshold: int = 80) -> list[dict]:
    """Remove near-duplicate news items, keeping highest-credibility source."""
    if not news_items:
        return []
    seen_titles = []
    result = []
    # Sort by source credibility (best first)
    sorted_items = sorted(
        news_items,
        key=lambda x: SOURCE_CREDIBILITY.get(x.get("source", ""), 1),
        reverse=True,
    )
    for item in sorted_items:
        title = item["title"]
        is_dup = False
        for seen in seen_titles:
            if fuzz.ratio(title.lower(), seen.lower()) > threshold:
                is_dup = True
                break
        if not is_dup:
            seen_titles.append(title)
            result.append(item)
    return result


# ── Main parsing ────────────────────────────────────────────────────────────

def parse_news_item(news_item: dict, markets: list[dict]) -> NewsSignal | None:
    """Analyze a single news item against available markets."""
    text = f"{news_item['title']} {news_item.get('summary', '')}"
    entities = extract_entities(text)
    if not entities:
        return None

    category = detect_category(text)
    sentiment = nuanced_sentiment(text)
    source = news_item.get("source", "unknown")
    importance = score_importance(news_item["title"], source)
    breaking = is_breaking(news_item["title"])

    matched = match_markets(entities, category, markets, threshold=75)
    if not matched:
        return None

    return NewsSignal(
        news_id=news_item["id"],
        news_title=news_item["title"],
        news_source=source,
        entities=entities,
        category=category,
        sentiment=sentiment,
        importance=importance,
        is_breaking=breaking,
        matched_markets=matched,
        published=news_item.get("published", ""),
    )


def parse_all(news_items: list[dict], markets: list[dict]) -> list[NewsSignal]:
    """Parse all news, deduplicate first, return signals."""
    deduped = deduplicate_news(news_items)
    signals = []
    for item in deduped:
        sig = parse_news_item(item, markets)
        if sig:
            signals.append(sig)
    return signals


def parse_with_llm(news_items: list[dict], markets: list[dict]) -> list[dict]:
    """Run LLM analysis and return LLM signals as enriched dicts.
    
    Returns list of dicts with keys: market_id, question, estimated_probability,
    confidence, direction, reasoning, news_title, source (='LLM').
    """
    from .llm_analyzer import analyze_news_batch

    try:
        llm_signals = analyze_news_batch(news_items, markets)
    except Exception as e:
        print(f"[LLM] Analysis failed, falling back to keywords: {e}")
        return []

    results = []
    for s in llm_signals:
        # Find the market to get prices/volume
        market = markets[s.market_index] if s.market_index < len(markets) else None
        if not market:
            continue
        prices = market.get("outcomePrices", [])
        results.append({
            "market_id": s.market_id,
            "question": s.market_question,
            "estimated_probability": s.estimated_probability,
            "confidence": s.confidence,
            "direction": s.direction,
            "reasoning": s.reasoning,
            "news_title": s.news_title,
            "source": "LLM",
            "current_yes": prices[0] if prices else 0.5,
            "volume": market.get("volume", 0),
        })
    return results

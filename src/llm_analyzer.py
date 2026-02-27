"""LLM-powered news analysis using Claude via OpenClaw sessions_spawn."""

import json
import re
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass


@dataclass
class LLMSignal:
    news_index: int
    market_index: int
    direction: str  # "YES_UP" or "YES_DOWN"
    estimated_probability: float
    confidence: float
    reasoning: str
    news_title: str = ""
    market_question: str = ""
    market_id: str = ""


DATA_DIR = Path(__file__).parent
LLM_REQUEST = DATA_DIR / "llm_request.json"
LLM_RESPONSE = DATA_DIR / "llm_response.json"


def build_prompt(news_items: list[dict], markets: list[dict]) -> str:
    news_section = "\n".join(
        f"  [{i}] {item.get('title', 'N/A')} (source: {item.get('source', 'unknown')})"
        for i, item in enumerate(news_items[:15])
    )
    market_section = "\n".join(
        f"  [{i}] {m.get('question', 'N/A')} | YES: {_yes_price(m)} | vol: ${m.get('volume', 0):,.0f}"
        for i, m in enumerate(markets[:20])
    )
    return f"""You are an expert Polymarket trading analyst. Analyze how breaking news affects prediction market prices.

CURRENT NEWS (last 2 hours):
{news_section}

ACTIVE POLYMARKET MARKETS:
{market_section}

For each news item that MEANINGFULLY affects any market, output a signal.
Rules:
- Only flag STRONG, DIRECT connections (not vague/tangential)
- estimated_probability is what YES should be worth (0.01-0.99)
- confidence is how sure you are (0.5-1.0)
- Skip news that doesn't clearly affect any listed market
- Consider: does this news make the event MORE or LESS likely?

Write the result as JSON to {LLM_RESPONSE}:
{{"signals": [{{"news_index": 0, "market_index": 5, "direction": "YES_UP", "estimated_probability": 0.75, "confidence": 0.8, "reasoning": "..."}}]}}
If no signals: {{"signals": []}}"""


def _yes_price(market: dict) -> str:
    prices = market.get("outcomePrices", [])
    if prices:
        return f"{float(prices[0]):.1%}"
    return "N/A"


def analyze_news_batch(news_items: list[dict], markets: list[dict]) -> list[LLMSignal]:
    """Read Claude's analysis from llm_response.json (written by OpenClaw cron job every 5 min)."""
    if not news_items or not markets:
        return []

    # Check if Claude has written a recent analysis
    if not LLM_RESPONSE.exists():
        print("[LLM] No Claude analysis yet (waiting for cron job)")
        return []

    # Check freshness (ignore if older than 10 minutes)
    age = time.time() - LLM_RESPONSE.stat().st_mtime
    if age > 600:
        print(f"[LLM] Claude analysis is stale ({age/60:.0f}min old)")
        return []

    try:
        response_text = LLM_RESPONSE.read_text().strip()
        data = _extract_json(response_text)
        if data and "signals" in data:
            signals = _parse_signals(data, news_items, markets)
            print(f"[LLM] Claude analysis: {len(signals)} signals ({age:.0f}s ago)")
            return signals
        else:
            print("[LLM] Claude response not parseable")
            return []
    except Exception as e:
        print(f"[LLM] Error reading Claude analysis: {e}")
        return []


def _extract_json(text: str) -> dict | None:
    """Extract JSON from markdown code blocks or raw text."""
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def _parse_signals(data: dict, news_items: list[dict], markets: list[dict]) -> list[LLMSignal]:
    """Parse JSON signals into LLMSignal objects.
    
    Uses market_id from LLM response to find the correct market (avoids index drift
    when market_cache.json changes between LLM cron run and scanner read).
    """
    # Build market lookup by condition_id
    market_by_id = {}
    for i, m in enumerate(markets):
        mid = m.get("id", "") or m.get("condition_id", "")
        if mid:
            market_by_id[mid] = (i, m)
    
    signals = []
    for s in data["signals"]:
        try:
            ni = int(s.get("news_index", 0))
            
            # Prefer market_id lookup over array index (index drifts between cron runs)
            market_id_from_llm = s.get("market_id", "")
            if market_id_from_llm and market_id_from_llm in market_by_id:
                mi, market = market_by_id[market_id_from_llm]
            else:
                mi = int(s.get("market_index", 0))
                if mi >= len(markets):
                    continue
                market = markets[mi]
            
            if ni >= len(news_items):
                ni = 0  # fallback; news_title from LLM is more reliable anyway
            
            signals.append(LLMSignal(
                news_index=ni,
                market_index=mi,
                direction=s.get("direction", "YES_UP"),
                estimated_probability=max(0.01, min(0.99, float(s.get("estimated_probability", 0.5)))),
                confidence=max(0.5, min(1.0, float(s.get("confidence", 0.5)))),
                reasoning=s.get("reasoning", ""),
                news_title=s.get("news_title", "") or news_items[ni].get("title", ""),
                market_question=s.get("question", "") or market.get("question", ""),
                market_id=market_id_from_llm or market.get("id", ""),
            ))
        except (KeyError, ValueError, IndexError):
            continue
    return signals

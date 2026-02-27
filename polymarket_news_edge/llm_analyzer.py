"""LLM-powered news analysis using Gemini CLI."""

import json
import re
import subprocess
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


GEMINI_BIN = "/opt/homebrew/bin/gemini"
GEMINI_MODEL = "gemini-2.5-flash"


def build_prompt(news_items: list[dict], markets: list[dict]) -> str:
    news_section = "\n".join(
        f"  [{i}] {item.get('title', 'N/A')} (source: {item.get('source', 'unknown')})"
        for i, item in enumerate(news_items[:20])
    )
    market_section = "\n".join(
        f"  [{i}] {m.get('question', 'N/A')} | YES: {_yes_price(m)} | vol: ${m.get('volume', 0):,.0f}"
        for i, m in enumerate(markets[:30])
    )
    return f"""You are an expert Polymarket trading analyst. Your job is to identify how breaking news affects prediction market prices.

CURRENT NEWS (last 2 hours):
{news_section}

ACTIVE POLYMARKET MARKETS:
{market_section}

TASK: For each news item that MEANINGFULLY affects any market, output a signal.
Rules:
- Only flag STRONG, DIRECT connections (not vague/tangential)
- estimated_probability is what YES should be worth (0.01-0.99)
- confidence is how sure you are (0.5-1.0)
- If a news item doesn't clearly affect any listed market, skip it
- Consider: does this news make the event MORE or LESS likely?

Return ONLY valid JSON, no other text:
{{"signals": [{{"news_index": 0, "market_index": 5, "direction": "YES_UP", "estimated_probability": 0.75, "confidence": 0.8, "reasoning": "..."}}]}}
If no signals, return {{"signals": []}}"""


def _yes_price(market: dict) -> str:
    prices = market.get("outcomePrices", [])
    if prices:
        return f"{float(prices[0]):.1%}"
    return "N/A"


def _extract_json(text: str) -> dict | None:
    """Extract JSON from markdown code blocks or raw text."""
    # Try markdown code block first
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Try to parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try finding JSON object in text
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def analyze_news_batch(news_items: list[dict], markets: list[dict]) -> list[LLMSignal]:
    """Call Gemini to analyze news against markets. Returns structured signals."""
    if not news_items or not markets:
        return []

    prompt = build_prompt(news_items, markets)

    try:
        result = subprocess.run(
            [GEMINI_BIN, "-m", GEMINI_MODEL, "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        print("[LLM] Gemini timed out (60s)")
        return []
    except FileNotFoundError:
        print(f"[LLM] Gemini binary not found at {GEMINI_BIN}")
        return []

    stdout = result.stdout.strip()
    if not stdout:
        print(f"[LLM] Empty response from Gemini (rc={result.returncode})")
        return []

    data = _extract_json(stdout)
    if not data or "signals" not in data:
        print(f"[LLM] Failed to parse Gemini response: {stdout[:200]}")
        return []

    signals = []
    for s in data["signals"]:
        try:
            ni = int(s["news_index"])
            mi = int(s["market_index"])
            if ni >= len(news_items) or mi >= len(markets):
                continue
            signals.append(LLMSignal(
                news_index=ni,
                market_index=mi,
                direction=s.get("direction", "YES_UP"),
                estimated_probability=max(0.01, min(0.99, float(s.get("estimated_probability", 0.5)))),
                confidence=max(0.5, min(1.0, float(s.get("confidence", 0.5)))),
                reasoning=s.get("reasoning", ""),
                news_title=news_items[ni].get("title", ""),
                market_question=markets[mi].get("question", ""),
                market_id=markets[mi].get("id", ""),
            ))
        except (KeyError, ValueError, IndexError):
            continue

    return signals

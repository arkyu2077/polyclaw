"""LLM-powered news analysis — file-based IPC or direct API call."""

import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass

from config import get_config


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


def build_prompt(news_items: list[dict], markets: list[dict],
                 matched_market_ids: set[str] | None = None) -> str:
    news_section = "\n".join(
        f"  [{i}] {item.get('title', 'N/A')} (source: {item.get('source', 'unknown')})"
        for i, item in enumerate(news_items[:15])
    )

    # Prioritize markets with keyword matches, cap at 12
    if matched_market_ids:
        priority = [m for m in markets if m.get("id") in matched_market_ids]
        other = [m for m in markets if m.get("id") not in matched_market_ids]
        ordered = (priority + other)[:12]
    else:
        ordered = markets[:12]

    market_section = "\n".join(
        f"  [{i}] {m.get('question', 'N/A')} | YES: {_yes_price(m)} | vol: ${m.get('volume', 0):,.0f}"
        for i, m in enumerate(ordered)
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

Output JSON:
{{"signals": [{{"news_index": 0, "market_index": 5, "direction": "YES_UP", "estimated_probability": 0.75, "confidence": 0.8, "reasoning": "..."}}]}}
If no signals: {{"signals": []}}"""


def _yes_price(market: dict) -> str:
    prices = market.get("outcomePrices", [])
    if prices:
        return f"{float(prices[0]):.1%}"
    return "N/A"


def analyze_news_batch(news_items: list[dict], markets: list[dict],
                       matched_market_ids: set[str] | None = None) -> list[LLMSignal]:
    """Analyze news via LLM — file-based IPC (cron) or direct API call."""
    cfg = get_config()
    if not news_items or not markets:
        return []

    if cfg.llm_provider == "file":
        return _read_from_file(news_items, markets)
    elif cfg.llm_provider in ("gemini", "openai", "anthropic"):
        return _call_api(news_items, markets, matched_market_ids, cfg)

    print(f"[LLM] Unknown provider: {cfg.llm_provider}")
    return []


def _read_from_file(news_items: list[dict], markets: list[dict]) -> list[LLMSignal]:
    """Read analysis from llm_response.json (written by cron job)."""
    if not LLM_RESPONSE.exists():
        print("[LLM] No analysis yet (waiting for cron job)")
        return []

    age = time.time() - LLM_RESPONSE.stat().st_mtime
    if age > 600:
        print(f"[LLM] Analysis is stale ({age/60:.0f}min old)")
        return []

    try:
        response_text = LLM_RESPONSE.read_text().strip()
        data = _extract_json(response_text)
        if data and "signals" in data:
            signals = _parse_signals(data, news_items, markets)
            print(f"[LLM] File analysis: {len(signals)} signals ({age:.0f}s ago)")
            return signals
        else:
            print("[LLM] Response not parseable")
            return []
    except Exception as e:
        print(f"[LLM] Error reading analysis: {e}")
        return []


def _call_api(news_items: list[dict], markets: list[dict],
              matched_market_ids: set[str] | None, cfg) -> list[LLMSignal]:
    """Call LLM API directly (Gemini, OpenAI, or Anthropic)."""
    import httpx

    prompt = build_prompt(news_items, markets, matched_market_ids)
    api_key = cfg.llm_api_key
    if not api_key:
        print("[LLM] No LLM_API_KEY set — skipping API call")
        return []

    model = cfg.llm_model
    try:
        if cfg.llm_provider == "gemini":
            model = model or "gemini-2.0-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            resp = httpx.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        elif cfg.llm_provider == "openai":
            model = model or "gpt-4o-mini"
            url = "https://api.openai.com/v1/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }
            resp = httpx.post(url, json=payload, timeout=60,
                              headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

        elif cfg.llm_provider == "anthropic":
            model = model or "claude-sonnet-4-6"
            url = "https://api.anthropic.com/v1/messages"
            payload = {
                "model": model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = httpx.post(url, json=payload, timeout=60, headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            })
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]
        else:
            return []

        data = _extract_json(text)
        if data and "signals" in data:
            signals = _parse_signals(data, news_items, markets)
            print(f"[LLM] {cfg.llm_provider}/{model}: {len(signals)} signals")
            # Cache response for dedup within same cycle
            LLM_RESPONSE.write_text(text)
            return signals
        else:
            print(f"[LLM] {cfg.llm_provider} response not parseable")
            return []

    except Exception as e:
        print(f"[LLM] API call failed ({cfg.llm_provider}): {e}")
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

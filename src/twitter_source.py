"""Twitter/X news source via RapidAPI (twitter-api45).
Searches for market-moving tweets and KOL timelines.
"""
import json
import time
from datetime import datetime, timezone

import httpx

from .config import get_config

BASE_URL = "https://twitter-api45.p.rapidapi.com"

# --- KOL accounts (high signal-to-noise ratio) ---
KOLS = {
    # Tier 1 — Breaking news bots (fastest sources on X)
    "DeItaone": 5,
    "WatcherGuru": 5,
    "unusual_whales": 4,
    "zerohedge": 4,
    "tier10k": 4,
    # Tier 2 — On-chain / whale tracking
    "lookonchain": 4,
    "whale_alert": 4,
    "EmberCN": 3,
    "ai_9684xtpa": 3,
    # Tier 3 — Macro / politics / regulation
    "FedGuy12": 3,
    "NickTimiraos": 4,
    "DesoGames": 3,
    "PolymarketBets": 3,
}

SEARCH_QUERIES = [
    'from:DeItaone OR from:WatcherGuru OR from:tier10k',
    'from:lookonchain OR from:whale_alert OR from:EmberCN',
    'from:unusual_whales OR from:zerohedge OR from:NickTimiraos',
    'from:ai_9684xtpa OR from:FedGuy12 OR from:PolymarketBets',
    '(bitcoin OR BTC) (breaking OR crash OR surge OR SEC OR ETF) -filter:replies',
    '(ethereum OR ETH) (breaking OR upgrade OR hack OR SEC) -filter:replies',
    '(Trump OR "executive order") (crypto OR tariff OR ban) -filter:replies',
    'SEC (crypto OR approve OR reject OR lawsuit) -filter:replies',
    '(CPI OR inflation OR "interest rate") (Fed OR breaking) -filter:replies',
    '"breaking news" (confirmed OR announced OR resigned OR killed) -filter:replies',
]


def _get_state_file():
    config = get_config()
    return config._data_path / "twitter_state.json"


def _load_state():
    state_file = _get_state_file()
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {"query_idx": 0, "key_idx": 0, "last_fetch": 0, "seen_ids": []}


def _save_state(state):
    state["seen_ids"] = state.get("seen_ids", [])[-500:]
    _get_state_file().write_text(json.dumps(state))


def _get_headers():
    """Rotate API keys round-robin."""
    config = get_config()
    keys = config.twitter_rapidapi_keys
    
    if not keys:
        raise ValueError("No Twitter RapidAPI keys configured")
    
    state = _load_state()
    idx = state.get("key_idx", 0) % len(keys)
    state["key_idx"] = idx + 1
    _save_state(state)
    
    return {
        "x-rapidapi-host": "twitter-api45.p.rapidapi.com",
        "x-rapidapi-key": keys[idx],
    }


def _tweet_to_news(tw: dict) -> dict:
    """Convert a raw tweet dict to our standard news format."""
    text = tw.get("text", "")
    likes = tw.get("favorites", 0) or 0
    rts = tw.get("retweets", 0) or 0
    engagement = likes + rts
    followers = tw.get("user_info", {}).get("followers_count", 0) or 0
    screen_name = tw.get("screen_name", "")

    kol_tier = KOLS.get(screen_name, 0)
    if kol_tier:
        importance = kol_tier
    elif engagement > 5000 or followers > 500000:
        importance = 5
    elif engagement > 1000 or followers > 100000:
        importance = 4
    elif engagement > 200 or followers > 20000:
        importance = 3
    else:
        importance = 2

    tweet_id = tw.get("tweet_id", tw.get("rest_id", ""))

    return {
        "title": text[:150],
        "summary": text,
        "source": "twitter",
        "source_detail": f"@{screen_name} ({engagement} eng, {followers} followers)",
        "published": tw.get("created_at", datetime.now(timezone.utc).isoformat()),
        "url": f"https://x.com/{screen_name}/status/{tweet_id}" if tweet_id else "",
        "importance": importance,
    }


def fetch_search(query: str = None, search_type: str = "Latest", max_results: int = 20) -> list[dict]:
    """Search tweets. Returns list of news dicts."""
    config = get_config()
    
    if not config.twitter_rapidapi_keys:
        return []
    
    state = _load_state()

    # Rate limit: min 3 min between calls
    if time.time() - state.get("last_fetch", 0) < 180:
        return []

    if query is None:
        idx = state["query_idx"] % len(SEARCH_QUERIES)
        query = SEARCH_QUERIES[idx]
        state["query_idx"] = idx + 1

    try:
        resp = httpx.get(
            f"{BASE_URL}/search.php",
            params={"query": query, "search_type": search_type},
            headers=_get_headers(),
            timeout=15,
        )
        state["last_fetch"] = time.time()

        if resp.status_code == 429:
            _save_state(state)
            return []
        resp.raise_for_status()

        data = resp.json()
        tweets = data.get("timeline", [])[:max_results]

        seen = set(state.get("seen_ids", []))
        results = []
        new_ids = []

        for tw in tweets:
            tid = tw.get("tweet_id", tw.get("rest_id", ""))
            if tid and tid in seen:
                continue
            new_ids.append(tid)
            results.append(_tweet_to_news(tw))

        state["seen_ids"] = list(seen | set(new_ids))
        _save_state(state)
        return results

    except Exception:
        _save_state(state)
        return []


def fetch_all() -> list[dict]:
    """Main entry: fetch one rotated search query. Called each scanner cycle."""
    return fetch_search()


if __name__ == "__main__":
    results = fetch_search("bitcoin breaking -filter:replies", "Latest")
    print(f"Fetched {len(results)} tweets")
    for r in results:
        print(f"  [{r['importance']}] {r['source_detail']}: {r['title'][:80]}")

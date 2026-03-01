"""GDELT global news source — free API, no key required."""
import json
import hashlib
import time
from datetime import datetime, timezone

import httpx

from .config import get_config

MIN_INTERVAL = 900  # 15 minutes
MAX_SEEN = 500

DEFAULT_QUERIES = [
    "polymarket OR prediction market",
    "trump OR biden",
    "bitcoin OR ethereum",
    "war OR conflict",
    "oil OR energy crisis",
]

DOMAIN_CREDIBILITY = {
    "reuters.com": 5, "apnews.com": 5, "bbc.com": 4, "bbc.co.uk": 4,
    "nytimes.com": 4, "washingtonpost.com": 4, "theguardian.com": 4,
    "aljazeera.com": 4, "france24.com": 4, "dw.com": 4,
    "cnbc.com": 4, "bloomberg.com": 5, "ft.com": 5,
    "cnn.com": 3, "foxnews.com": 3,
}


def _state_file():
    return get_config()._data_path / "gdelt_state.json"


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "seen_ids": []}


def _save_state(state):
    state["seen_ids"] = state.get("seen_ids", [])[-MAX_SEEN:]
    _state_file().write_text(json.dumps(state))


def _domain_importance(url: str) -> int:
    for domain, score in DOMAIN_CREDIBILITY.items():
        if domain in url:
            return score
    return 2


def fetch_gdelt(queries=None) -> list[dict]:
    """Fetch articles from GDELT API. Free, no API key needed."""
    state = _load_state()
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    queries = queries or DEFAULT_QUERIES
    seen = set(state.get("seen_ids", []))
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    for query in queries:
        try:
            resp = httpx.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": query,
                    "mode": "artlist",
                    "maxrecords": "30",
                    "format": "json",
                    "timespan": "1h",
                    "sort": "DateDesc",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            articles = data.get("articles", [])

            for art in articles:
                url = art.get("url", "")
                title = art.get("title", "")
                if not title:
                    continue

                aid = hashlib.sha256(url.encode()).hexdigest()[:24]
                if aid in seen:
                    continue
                seen.add(aid)

                items.append({
                    "id": f"gdelt-{aid}",
                    "title": title,
                    "summary": art.get("seendate", ""),
                    "source": "gdelt",
                    "published": art.get("seendate", ts),
                    "fetched_at": ts,
                    "url": url,
                    "importance": _domain_importance(url),
                })
        except Exception:
            continue

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state)
    return items

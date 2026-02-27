"""Reddit news source — public JSON API, no auth needed."""
import httpx
import json
import os
import time
from datetime import datetime, timezone

SUBREDDITS = ["wallstreetbets", "politics", "cryptocurrency", "worldnews", "sports"]
STATE_FILE = os.path.join(os.path.dirname(__file__), "reddit_state.json")
HEADERS = {"User-Agent": "PolymarketBot/1.0"}
MIN_INTERVAL = 300  # 5 minutes between full fetches
MAX_AGE_HOURS = 6


def _load_state(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"last_fetch": 0, "seen_ids": []}


def _save_state(state, state_file):
    state["seen_ids"] = state.get("seen_ids", [])[-500:]
    with open(state_file, "w") as f:
        json.dump(state, f)


def _calc_importance(score: int, comments: int) -> int:
    total = score + comments * 2
    if total > 5000:
        return 5
    if total > 2000:
        return 4
    if total > 500:
        return 3
    if total > 200:
        return 2
    return 1


def fetch_reddit(state_file: str = STATE_FILE) -> list[dict]:
    """Fetch hot posts from Reddit. Returns news-format dicts."""
    state = _load_state(state_file)
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    seen = set(state.get("seen_ids", []))
    cutoff = now - MAX_AGE_HOURS * 3600
    items = []

    for sub in SUBREDDITS:
        try:
            resp = httpx.get(
                f"https://www.reddit.com/r/{sub}/hot.json?limit=25",
                headers=HEADERS, timeout=6, follow_redirects=True,
            )
            resp.raise_for_status()
            posts = resp.json().get("data", {}).get("children", [])

            for post in posts:
                d = post.get("data", {})
                pid = d.get("id", "")
                if pid in seen:
                    continue
                score = d.get("score", 0)
                comments = d.get("num_comments", 0)
                created = d.get("created_utc", 0)

                if created < cutoff:
                    continue
                if score < 50 and comments < 20:
                    continue

                seen.add(pid)
                items.append({
                    "title": d.get("title", ""),
                    "source": f"reddit:r/{sub}",
                    "url": f"https://reddit.com{d.get('permalink', '')}",
                    "score": score,
                    "comments": comments,
                    "timestamp": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
                    "importance": _calc_importance(score, comments),
                })
        except Exception:
            continue

        time.sleep(2)

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state, state_file)
    return items


if __name__ == "__main__":
    results = fetch_reddit()
    for r in results:
        print(f"[{r['importance']}] {r['source']}: {r['title'][:80]}")
    print(f"\nTotal: {len(results)} items")

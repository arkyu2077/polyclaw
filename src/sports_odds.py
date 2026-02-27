"""Sports odds source — The Odds API (free tier, optional key)."""
import httpx
import json
import os
import time
from datetime import datetime, timezone

SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_epl",
]
SPORT_LABELS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "soccer_epl": "EPL",
}
STATE_FILE = os.path.join(os.path.dirname(__file__), "odds_state.json")
MIN_INTERVAL = 1800  # 30 minutes (conserve 500 req/month)


def _load_state(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"last_fetch": 0, "seen_ids": []}


def _save_state(state, state_file):
    state["seen_ids"] = state.get("seen_ids", [])[-200:]
    with open(state_file, "w") as f:
        json.dump(state, f)


def _american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def fetch_sports_odds(api_key: str | None = None, state_file: str = STATE_FILE) -> list[dict]:
    """Fetch sports odds. Requires ODDS_API_KEY env var or api_key param."""
    key = api_key or os.environ.get("ODDS_API_KEY", "")
    if not key:
        return []  # Skip silently if no key

    state = _load_state(state_file)
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    seen = set(state.get("seen_ids", []))
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    for sport in SPORTS:
        try:
            resp = httpx.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                params={
                    "apiKey": key,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "american",
                },
                timeout=6,
            )
            if resp.status_code == 401:
                return []  # Bad key, stop all
            if resp.status_code != 200:
                continue

            games = resp.json()
            label = SPORT_LABELS.get(sport, sport)

            for game in games:
                gid = game.get("id", "")
                if gid in seen:
                    continue
                seen.add(gid)

                home = game.get("home_team", "")
                away = game.get("away_team", "")
                bookmakers = game.get("bookmakers", [])
                if not bookmakers:
                    continue

                # Average home win probability across bookmakers
                probs = []
                for bk in bookmakers:
                    for market in bk.get("markets", []):
                        if market.get("key") != "h2h":
                            continue
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name") == home:
                                try:
                                    probs.append(_american_to_prob(int(outcome["price"])))
                                except (ValueError, KeyError):
                                    pass

                if not probs:
                    continue

                avg_prob = sum(probs) / len(probs)
                pct = avg_prob * 100

                title = f"{label}: {away} @ {home} — Vegas implied: {home} {pct:.0f}%"
                # Higher importance for closer matchups (more interesting for betting)
                importance = 2
                if 40 < pct < 60:
                    importance = 3  # Toss-up games are more interesting

                items.append({
                    "title": title,
                    "source": f"odds:{label.lower()}",
                    "importance": importance,
                    "timestamp": ts,
                })
        except Exception:
            continue

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state, state_file)
    return items


if __name__ == "__main__":
    results = fetch_sports_odds()
    if not results:
        print("No results (set ODDS_API_KEY env var)")
    for r in results:
        print(f"[{r['importance']}] {r['source']}: {r['title']}")
    print(f"\nTotal: {len(results)} items")

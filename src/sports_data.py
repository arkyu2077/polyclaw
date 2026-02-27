"""Sports data source — ESPN API for injury reports, game results, etc."""

import json
import hashlib
from datetime import datetime, timezone

import httpx

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports"

LEAGUES = {
    "nba": "basketball/nba",
    "nfl": "football/nfl",
    "mlb": "baseball/mlb",
    "soccer": "soccer/usa.1",  # MLS
}


def _item_id(title: str, source: str) -> str:
    return hashlib.sha256(f"{source}:{title}".encode()).hexdigest()[:32]


def fetch_scoreboard(league: str) -> list[dict]:
    """Fetch current scoreboard for a league."""
    items = []
    try:
        path = LEAGUES.get(league, league)
        resp = httpx.get(
            f"{ESPN_API}/{path}/scoreboard",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        
        for event in data.get("events", [])[:10]:
            name = event.get("name", "")
            status = event.get("status", {}).get("type", {}).get("description", "")
            
            # Get teams and scores
            teams_data = event.get("competitions", [{}])[0].get("competitors", [])
            if len(teams_data) >= 2:
                home = teams_data[0]
                away = teams_data[1]
                score_text = f"{away.get('team', {}).get('abbreviation', '?')} {away.get('score', '?')} @ {home.get('team', {}).get('abbreviation', '?')} {home.get('score', '?')}"
            else:
                score_text = name
            
            title = f"[ESPN-{league.upper()}] {score_text} — {status}"
            
            items.append({
                "id": _item_id(title, f"ESPN-{league.upper()}"),
                "source": f"ESPN-{league.upper()}",
                "title": title,
                "summary": title,
                "published": datetime.now(timezone.utc).isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "url": f"https://www.espn.com/{league}/scoreboard",
                "importance": 3,
            })
    except Exception as e:
        print(f"  [warn] ESPN {league}: {e}")
    
    return items


def fetch_injuries(league: str) -> list[dict]:
    """Fetch injury reports for a league."""
    items = []
    try:
        path = LEAGUES.get(league, league)
        resp = httpx.get(
            f"{ESPN_API}/{path}/injuries",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        
        for team in data.get("teams", [])[:5]:
            team_name = team.get("displayName", "")
            for injury in team.get("injuries", [])[:3]:
                athlete = injury.get("athlete", {}).get("displayName", "?")
                status = injury.get("status", "")
                description = injury.get("description", "")
                
                title = f"[ESPN-{league.upper()}] {team_name}: {athlete} — {status}"
                
                items.append({
                    "id": _item_id(title, f"ESPN-{league.upper()}"),
                    "source": f"ESPN-{league.upper()}",
                    "title": title,
                    "summary": f"{title}. {description}",
                    "published": datetime.now(timezone.utc).isoformat(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "url": f"https://www.espn.com/{league}/injuries",
                    "importance": 4,  # Injuries are high importance for betting
                })
    except Exception as e:
        # Injuries endpoint may not exist for all leagues
        pass
    
    return items


def fetch_all_sports() -> list[dict]:
    """Fetch all sports data."""
    items = []
    for league in LEAGUES.keys():
        items.extend(fetch_scoreboard(league))
        items.extend(fetch_injuries(league))
    return items


if __name__ == "__main__":
    items = fetch_all_sports()
    print(f"Fetched {len(items)} sports items")
    for item in items[:10]:
        print(f"  [{item['importance']}] {item['title']}")

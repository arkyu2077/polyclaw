"""Economic calendar data source — ForexFactory JSON feed.
Tracks CPI, NFP, FOMC, GDP, etc. High-impact events create signals.
"""
import json
import time
from datetime import datetime, timezone, timedelta

import httpx

from .config import get_config

CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# High-impact event keywords that move Polymarket markets
HIGH_IMPACT_KEYWORDS = [
    "cpi", "inflation", "nonfarm", "non-farm", "employment", "unemployment",
    "fomc", "interest rate", "federal funds", "fed chair", "powell",
    "gdp", "retail sales", "pce", "consumer confidence", "ism manufacturing",
    "initial claims", "jobless claims", "trade balance", "housing starts",
    "core pce", "ppi", "consumer price",
]


def _get_cache_file():
    config = get_config()
    return config._data_path / "econ_calendar_cache.json"


def _load_cache():
    try:
        return json.loads(_get_cache_file().read_text())
    except Exception:
        return {"events": [], "last_fetch": 0}


def _save_cache(cache):
    _get_cache_file().write_text(json.dumps(cache, indent=2))


def fetch_calendar() -> list[dict]:
    """Fetch economic calendar. Returns news-format dicts for upcoming high-impact events."""
    cache = _load_cache()

    # Refresh every 30 min
    if time.time() - cache.get("last_fetch", 0) < 1800:
        return _upcoming_alerts(cache.get("events", []))

    all_events = []
    for url in CALENDAR_URLS:
        try:
            resp = httpx.get(url, timeout=10)
            resp.raise_for_status()
            events = resp.json()
            all_events.extend(events)
        except Exception:
            continue

    cache["events"] = all_events
    cache["last_fetch"] = time.time()
    _save_cache(cache)

    return _upcoming_alerts(all_events)


def _upcoming_alerts(events: list) -> list[dict]:
    """Filter events to those happening in the next 24h that are high-impact."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=24)
    alerts = []

    for ev in events:
        try:
            date_str = ev.get("date", "")
            if not date_str:
                continue
            ev_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone(timedelta(hours=-5)))
            ev_utc = ev_time.astimezone(timezone.utc)

            if ev_utc < now - timedelta(minutes=30) or ev_utc > window_end:
                continue

            title = ev.get("title", "")
            impact = ev.get("impact", "").lower()
            country = ev.get("country", "")

            is_keyword = any(kw in title.lower() for kw in HIGH_IMPACT_KEYWORDS)
            is_high_impact = impact in ("high", "medium")

            if not (is_keyword or is_high_impact):
                continue

            if impact == "high" or is_keyword:
                importance = 5
            elif impact == "medium":
                importance = 3
            else:
                importance = 2

            hours_until = (ev_utc - now).total_seconds() / 3600
            time_label = f"in {hours_until:.1f}h" if hours_until > 0 else "NOW/just released"

            forecast = ev.get("forecast", "")
            previous = ev.get("previous", "")
            actual = ev.get("actual", "")

            summary = f"[ECON] {title} ({country}) — {time_label}"
            if actual:
                summary += f" | Actual: {actual} vs Forecast: {forecast} (Prev: {previous})"
            elif forecast:
                summary += f" | Forecast: {forecast} (Prev: {previous})"

            alerts.append({
                "title": summary,
                "summary": summary,
                "source": "economic_calendar",
                "source_detail": f"ForexFactory ({impact} impact)",
                "published": date_str,
                "url": "https://www.forexfactory.com/calendar",
                "importance": importance,
                "_event_time": ev_utc.isoformat(),
                "_hours_until": round(hours_until, 2),
                "_actual": actual,
                "_forecast": forecast,
                "_previous": previous,
            })

        except Exception:
            continue

    return alerts


if __name__ == "__main__":
    alerts = fetch_calendar()
    print(f"Upcoming high-impact events: {len(alerts)}")
    for a in alerts:
        print(f"  [{a['importance']}] {a['title']}")

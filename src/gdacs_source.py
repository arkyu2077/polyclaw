"""GDACS disaster alert source — free RSS feed, no key required."""
import json
import hashlib
import time
from datetime import datetime, timezone

import feedparser
import httpx

from .config import get_config

MIN_INTERVAL = 1800  # 30 minutes
GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"


def _state_file():
    return get_config()._data_path / "gdacs_state.json"


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "seen_ids": []}


def _save_state(state):
    state["seen_ids"] = state.get("seen_ids", [])[-200:]
    _state_file().write_text(json.dumps(state))


def _parse_alert_level(entry) -> str:
    """Extract alert level from GDACS RSS namespace."""
    # Try gdacs namespace attributes
    for attr in ("gdacs_alertlevel", "gdacs:alertlevel"):
        val = entry.get(attr, "")
        if val:
            return val.strip().lower()
    # Fallback: look in title or description
    title = entry.get("title", "").lower()
    if "red" in title:
        return "red"
    if "orange" in title:
        return "orange"
    if "green" in title:
        return "green"
    return "unknown"


def _parse_event_type(entry) -> str:
    for attr in ("gdacs_eventtype", "gdacs:eventtype"):
        val = entry.get(attr, "")
        if val:
            return val.strip()
    return ""


def _parse_country(entry) -> str:
    for attr in ("gdacs_country", "gdacs:country"):
        val = entry.get(attr, "")
        if val:
            return val.strip()
    return ""


def fetch_gdacs() -> list[dict]:
    """Fetch disaster alerts from GDACS RSS. Only Orange and Red alerts."""
    state = _load_state()
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    seen = set(state.get("seen_ids", []))
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    try:
        resp = httpx.get(GDACS_RSS, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        feed = feedparser.parse(resp.text)

        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title:
                continue

            aid = hashlib.sha256(f"gdacs:{title}".encode()).hexdigest()[:24]
            if aid in seen:
                continue

            level = _parse_alert_level(entry)
            # Only Orange and Red (Green is too noisy)
            if level not in ("red", "orange"):
                continue

            seen.add(aid)

            event_type = _parse_event_type(entry)
            country = _parse_country(entry)
            importance = 5 if level == "red" else 4

            summary_parts = []
            if event_type:
                summary_parts.append(f"Type: {event_type}")
            if country:
                summary_parts.append(f"Country: {country}")
            summary_parts.append(f"Alert: {level.upper()}")

            items.append({
                "id": f"gdacs-{aid}",
                "title": title,
                "summary": " | ".join(summary_parts),
                "source": "gdacs",
                "published": entry.get("published", ts),
                "fetched_at": ts,
                "url": link,
                "importance": importance,
            })
    except Exception:
        pass

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state)
    return items

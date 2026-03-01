"""Telegram channel monitor — RSS proxy for public channels, no API key needed."""
import json
import hashlib
import re
import time
from datetime import datetime, timezone

import feedparser
import httpx

from .config import get_config

MIN_INTERVAL = 600  # 10 minutes
MAX_PER_CHANNEL = 5

CHANNELS = [
    "bbcbreaking",
    "ReutersWorld",
    "CNN_Breaking",
    "FinancialTimes",
    "inikiforov",
    "osaborona",
    "ukaborona",
    "oslobon",
    "foraboronda",
]

RSSHUB_BASE = "https://rsshub.app/telegram/channel"
FALLBACK_BASE = "https://t.me/s"


def _state_file():
    return get_config()._data_path / "telegram_state.json"


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "seen_ids": []}


def _save_state(state):
    state["seen_ids"] = state.get("seen_ids", [])[-500:]
    _state_file().write_text(json.dumps(state))


def _strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text).strip()


def _fetch_via_rsshub(channel: str) -> list[dict]:
    """Fetch channel via RSSHub proxy."""
    url = f"{RSSHUB_BASE}/{channel}"
    resp = httpx.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code != 200:
        return []
    feed = feedparser.parse(resp.text)
    return feed.entries[:MAX_PER_CHANNEL]


def _fetch_via_html(channel: str) -> list[dict]:
    """Fallback: scrape t.me/s/ public preview."""
    url = f"{FALLBACK_BASE}/{channel}"
    resp = httpx.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code != 200:
        return []

    items = []
    # Extract message blocks from HTML
    messages = re.findall(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        resp.text,
        re.DOTALL,
    )
    for msg in messages[-MAX_PER_CHANNEL:]:
        text = _strip_html(msg)[:500]
        if text:
            items.append({"title": text[:200], "summary": text, "link": url})
    return items


def fetch_telegram() -> list[dict]:
    """Fetch latest messages from Telegram channels via RSS proxy."""
    state = _load_state()
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    seen = set(state.get("seen_ids", []))
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    for channel in CHANNELS:
        try:
            entries = _fetch_via_rsshub(channel)
            use_fallback = not entries
        except Exception:
            use_fallback = True
            entries = []

        if use_fallback:
            try:
                fallback = _fetch_via_html(channel)
                for fb in fallback:
                    title = fb.get("title", "")
                    if not title:
                        continue
                    aid = hashlib.sha256(f"tg:{channel}:{title[:80]}".encode()).hexdigest()[:24]
                    if aid in seen:
                        continue
                    seen.add(aid)
                    items.append({
                        "id": f"tg-{aid}",
                        "title": f"[{channel}] {title[:200]}",
                        "summary": fb.get("summary", "")[:500],
                        "source": "telegram",
                        "published": ts,
                        "fetched_at": ts,
                        "url": fb.get("link", f"https://t.me/{channel}"),
                        "importance": 2,
                    })
                continue
            except Exception:
                continue

        for entry in entries:
            title = entry.get("title", "")
            if not title:
                continue
            link = entry.get("link", f"https://t.me/{channel}")
            aid = hashlib.sha256(f"tg:{channel}:{title[:80]}".encode()).hexdigest()[:24]
            if aid in seen:
                continue
            seen.add(aid)

            summary = _strip_html(entry.get("summary", entry.get("description", "")))[:500]

            items.append({
                "id": f"tg-{aid}",
                "title": f"[{channel}] {title[:200]}",
                "summary": summary,
                "source": "telegram",
                "published": entry.get("published", ts),
                "fetched_at": ts,
                "url": link,
                "importance": 2,
            })

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state)
    return items

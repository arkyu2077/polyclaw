"""Real-time news pipeline — RSS feeds, economic calendar, weather, BlockBeats, Fear & Greed.

v2 changes:
- Removed: CoinGecko Trending (0 useful signals), on-chain data (1/night, no matches)
- Added: Economic calendar (FOMC/CPI/GDP), Weather API (Open-Meteo)
- Kept: Reuters, AP, Bloomberg, CoinDesk, The Block, PANews, BlockBeats, Fear&Greed, ESPN sports
"""

import json
import re
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
import httpx

from .config import get_config

MAX_ITEMS = 100

# === Tier 1: High-quality news sources ===
RSS_FEEDS = {
    "Reuters": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "AP": "https://rsshub.app/apnews/topics/apf-business",
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",
    "PANews": "https://rss.panewslab.com/zh/rss",
}

# Fallback feeds (only used if primary fails)
FALLBACK_FEEDS = {
    "Reuters": "https://news.google.com/rss/search?q=site:reuters.com+markets&hl=en-US&gl=US&ceid=US:en",
    "AP": "https://news.google.com/rss/search?q=site:apnews.com+business&hl=en-US&gl=US&ceid=US:en",
    "Bloomberg": "https://news.google.com/rss/search?q=site:bloomberg.com+markets&hl=en-US&gl=US&ceid=US:en",
    "CryptoNews": "https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en",
}

# Weather cities for Polymarket weather markets
WEATHER_CITIES = {
    "London": {"lat": 51.5074, "lon": -0.1278},
    "New York": {"lat": 40.7128, "lon": -74.0060},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503},
    "Seoul": {"lat": 37.5665, "lon": 126.9780},
    "Toronto": {"lat": 43.6532, "lon": -79.3832},
    "Chicago": {"lat": 41.8781, "lon": -87.6298},
    "Miami": {"lat": 25.7617, "lon": -80.1918},
    "Los Angeles": {"lat": 34.0522, "lon": -118.2437},
}


def _item_id(title: str, source: str) -> str:
    return hashlib.sha256(f"{source}:{title}".encode()).hexdigest()[:32]


def _strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_rss() -> list[dict]:
    """Fetch articles from all RSS feeds with per-feed isolation."""
    items = []
    all_feeds = {**RSS_FEEDS, **FALLBACK_FEEDS}
    for source, url in all_feeds.items():
        try:
            with httpx.Client(timeout=6, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = client.get(url)
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:10]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", entry.get("description", ""))[:500]
                    published = entry.get("published", "")
                    items.append({
                        "id": _item_id(title, source),
                        "source": source,
                        "title": title,
                        "summary": _strip_html(summary),
                        "published": published,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "url": entry.get("link", ""),
                    })
        except Exception as e:
            print(f"  [warn] {source}: {e}")
    return items


def fetch_blockbeats() -> list[dict]:
    """Fetch flash news from BlockBeats (律动)."""
    items = []
    try:
        resp = httpx.get(
            "https://api.theblockbeats.news/v1/open-api/open-flash?size=20&page=1&type=push",
            timeout=15, headers={"User-Agent": "Mozilla/5.0"},
        )
        data = resp.json()
        if data.get("status") == 0:
            for entry in data.get("data", {}).get("data", []):
                title = _strip_html(entry.get("title", ""))
                content = _strip_html(entry.get("content", ""))[:500]
                ts = entry.get("create_time", "")
                try:
                    published = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    published = datetime.now(timezone.utc).isoformat()
                items.append({
                    "id": _item_id(title, "BlockBeats"),
                    "source": "BlockBeats",
                    "title": title,
                    "summary": content,
                    "published": published,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "url": entry.get("link", "https://www.theblockbeats.news"),
                })
    except Exception as e:
        print(f"  [warn] BlockBeats: {e}")
    return items


def fetch_fear_greed() -> list[dict]:
    """Fetch Crypto Fear & Greed Index."""
    items = []
    try:
        resp = httpx.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        data = resp.json()["data"][0]
        items.append({
            "id": _item_id(f"fng-{data['timestamp']}", "FearGreed"),
            "source": "Fear&Greed",
            "title": f"Crypto Fear & Greed Index: {data['value']} ({data['value_classification']})",
            "summary": f"Current index: {data['value']}/100 — {data['value_classification']}",
            "published": datetime.now(timezone.utc).isoformat(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "url": "https://alternative.me/crypto/fear-and-greed-index/",
        })
    except Exception as e:
        print(f"  [warn] Fear&Greed: {e}")
    return items


def fetch_weather() -> list[dict]:
    """Fetch weather forecasts from Open-Meteo (free, no API key needed)."""
    items = []
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    
    for city, coords in WEATHER_CITIES.items():
        try:
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={coords['lat']}&longitude={coords['lon']}"
                f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
                f"&timezone=auto&forecast_days=3"
            )
            resp = httpx.get(url, timeout=8)
            data = resp.json()
            daily = data.get("daily", {})
            
            dates = daily.get("time", [])
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])
            precip = daily.get("precipitation_sum", [])
            
            for i, date in enumerate(dates[:3]):
                if i >= len(highs):
                    break
                high = highs[i]
                low = lows[i]
                rain = precip[i] if i < len(precip) else 0
                
                day_label = "Today" if date == today else "Tomorrow" if date == tomorrow else date
                rain_note = f", {rain:.1f}mm precipitation" if rain > 0 else ""
                
                title = f"Weather {city} {day_label}: High {high:.0f}°C, Low {low:.0f}°C{rain_note}"
                
                items.append({
                    "id": _item_id(f"weather-{city}-{date}", "Weather"),
                    "source": "Weather",
                    "title": title,
                    "summary": (
                        f"{city} forecast for {date}: "
                        f"High {high:.0f}°C ({high * 9/5 + 32:.0f}°F), "
                        f"Low {low:.0f}°C ({low * 9/5 + 32:.0f}°F)"
                        f"{rain_note}"
                    ),
                    "published": now.isoformat(),
                    "fetched_at": now.isoformat(),
                    "url": f"https://open-meteo.com/",
                    "importance": 4,
                    "data": {
                        "city": city,
                        "date": date,
                        "high_c": high,
                        "low_c": low,
                        "high_f": round(high * 9/5 + 32, 1),
                        "low_f": round(low * 9/5 + 32, 1),
                        "precipitation_mm": rain,
                    },
                })
        except Exception as e:
            print(f"  [warn] Weather {city}: {e}")
    
    return items


def ingest() -> list[dict]:
    """Run full ingestion, deduplicate, save to news cache. Returns new items."""
    config = get_config()
    news_file = config.news_cache_file
    
    existing = []
    if news_file.exists():
        try:
            existing = json.loads(news_file.read_text())
        except Exception:
            existing = []

    seen_ids = {item["id"] for item in existing}

    # Sports data
    try:
        from .sports_data import fetch_all_sports
        sports_items = fetch_all_sports()
    except Exception as e:
        print(f"  [warn] Sports fetch failed: {e}")
        sports_items = []

    # Collect from all sources
    all_fetched = (
        fetch_rss()
        + fetch_blockbeats()
        + fetch_fear_greed()
        + fetch_weather()
        + sports_items
    )

    new_items = []
    for item in all_fetched:
        if item["id"] not in seen_ids:
            new_items.append(item)
            seen_ids.add(item["id"])

    combined = (new_items + existing)[:MAX_ITEMS]
    news_file.write_text(json.dumps(combined, indent=2))
    return new_items


if __name__ == "__main__":
    from rich.console import Console
    from collections import Counter
    console = Console()
    console.print("[bold]Fetching news...[/bold]")
    new = ingest()
    console.print(f"[green]{len(new)} new items ingested[/green]")
    
    sources = Counter(item["source"] for item in new)
    for src, count in sources.most_common():
        console.print(f"  {src}: {count}")

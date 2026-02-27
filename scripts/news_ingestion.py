"""Real-time news pipeline — RSS feeds, CoinGecko trending, Fear & Greed Index, Chinese crypto media."""

import json
import re
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import feedparser
import httpx

DATA_DIR = Path(__file__).parent
NEWS_FILE = DATA_DIR / "news_feed.json"
MAX_ITEMS = 100

RSS_FEEDS = {
    "Reuters": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "AP": "https://rsshub.app/apnews/topics/apf-business",
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",
    "PANews": "https://rss.panewslab.com/zh/rss",
}

# Fallback/alternative feeds if primary ones fail
FALLBACK_FEEDS = {
    "Reuters": "https://news.google.com/rss/search?q=site:reuters.com+markets&hl=en-US&gl=US&ceid=US:en",
    "AP": "https://news.google.com/rss/search?q=site:apnews.com+business&hl=en-US&gl=US&ceid=US:en",
    "Bloomberg": "https://news.google.com/rss/search?q=site:bloomberg.com+markets&hl=en-US&gl=US&ceid=US:en",
    "CryptoNews": "https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en",
}


def _item_id(title: str, source: str) -> str:
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()


def fetch_rss() -> list[dict]:
    """Fetch articles from all RSS feeds."""
    items = []
    client = httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})

    all_feeds = {**RSS_FEEDS, **FALLBACK_FEEDS}
    for source, url in all_feeds.items():
        try:
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
                    "summary": summary,
                    "published": published,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "url": entry.get("link", ""),
                })
        except Exception as e:
            print(f"  [warn] {source}: {e}")
    client.close()
    return items


def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_blockbeats() -> list[dict]:
    """Fetch flash news from BlockBeats (律动)."""
    items = []
    try:
        resp = httpx.get(
            "https://api.theblockbeats.news/v1/open-api/open-flash?size=20&page=1&type=push",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
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


def fetch_coingecko_trending() -> list[dict]:
    """Fetch CoinGecko trending coins as pseudo-news items."""
    items = []
    try:
        resp = httpx.get("https://api.coingecko.com/api/v3/search/trending", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        for coin in data.get("coins", [])[:5]:
            c = coin["item"]
            items.append({
                "id": _item_id(c["name"], "CoinGecko"),
                "source": "CoinGecko-Trending",
                "title": f"Trending: {c['name']} ({c['symbol']}) — rank #{c.get('market_cap_rank', '?')}",
                "summary": f"{c['name']} is trending on CoinGecko. Score: {c.get('score', 'N/A')}",
                "published": datetime.now(timezone.utc).isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "url": f"https://www.coingecko.com/en/coins/{c['id']}",
            })
    except Exception as e:
        print(f"  [warn] CoinGecko: {e}")
    return items


def fetch_fear_greed() -> list[dict]:
    """Fetch Fear & Greed Index."""
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


def ingest() -> list[dict]:
    """Run full ingestion, deduplicate, save to news_feed.json. Returns new items."""
    existing = []
    if NEWS_FILE.exists():
        try:
            existing = json.loads(NEWS_FILE.read_text())
        except Exception:
            existing = []

    seen_ids = {item["id"] for item in existing}

    new_items = []
    for item in fetch_rss() + fetch_blockbeats() + fetch_coingecko_trending() + fetch_fear_greed():
        if item["id"] not in seen_ids:
            new_items.append(item)
            seen_ids.add(item["id"])

    combined = (new_items + existing)[:MAX_ITEMS]
    NEWS_FILE.write_text(json.dumps(combined, indent=2))
    return new_items


if __name__ == "__main__":
    from rich.console import Console
    console = Console()
    console.print("[bold]Fetching news...[/bold]")
    new = ingest()
    console.print(f"[green]{len(new)} new items ingested[/green]")

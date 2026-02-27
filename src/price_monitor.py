"""Polymarket price anomaly detection — detect rapid price movements that signal insider info."""

import json
import hashlib
from datetime import datetime, timezone

from .config import get_config

# Thresholds
JUMP_THRESHOLD_5MIN = 0.05    # 5% move in 5 min = anomaly
JUMP_THRESHOLD_15MIN = 0.08   # 8% move in 15 min
VOLUME_SPIKE_MULT = 3.0       # 3x average volume = spike


def _item_id(title: str) -> str:
    return hashlib.md5(f"price-alert:{title}".encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_price_history() -> dict:
    """Load price history: {market_id: [{timestamp, yes_price, volume}, ...]}"""
    config = get_config()
    history_file = config.price_history_file
    if history_file.exists():
        try:
            return json.loads(history_file.read_text())
        except Exception:
            pass
    return {}


def save_price_history(history: dict):
    config = get_config()
    trimmed = {}
    for mid, entries in history.items():
        trimmed[mid] = entries[-100:]
    config.price_history_file.write_text(json.dumps(trimmed))


def record_and_detect(markets: list[dict]) -> list[dict]:
    """
    Record current prices and detect anomalies.
    Returns list of pseudo-news items for detected anomalies.
    """
    history = load_price_history()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    alerts = []

    for market in markets:
        if isinstance(market, str):
            continue
        mid = market.get("id", "")
        question = market.get("question", "")
        
        try:
            prices = market.get("outcomePrices", [])
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not prices:
                continue
            yes_price = float(prices[0])
        except (ValueError, TypeError, IndexError):
            continue

        try:
            volume = float(market.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0

        if mid not in history:
            history[mid] = []
        history[mid].append({
            "t": now_iso,
            "p": round(yes_price, 4),
            "v": round(volume, 0),
        })

        entries = history[mid]
        if len(entries) < 2:
            continue

        for lookback_label, lookback_seconds, threshold in [
            ("5min", 300, JUMP_THRESHOLD_5MIN),
            ("15min", 900, JUMP_THRESHOLD_15MIN),
        ]:
            target_time = now.timestamp() - lookback_seconds
            old_entry = None
            for entry in reversed(entries[:-1]):
                try:
                    entry_time = datetime.fromisoformat(entry["t"]).timestamp()
                    if entry_time <= target_time:
                        old_entry = entry
                        break
                except (ValueError, KeyError):
                    continue

            if old_entry is None:
                continue

            old_price = old_entry["p"]
            if old_price < 0.02 or old_price > 0.98:
                continue

            price_change = yes_price - old_price
            pct_change = abs(price_change) / old_price

            if pct_change >= threshold:
                direction = "surged" if price_change > 0 else "dropped"
                title = (f"🚨 Price Alert: {question[:50]} {direction} "
                        f"{abs(price_change):.1%} in {lookback_label}")

                alerts.append({
                    "id": _item_id(f"{mid}-{lookback_label}-{now.strftime('%H%M')}"),
                    "source": "Price-Alert",
                    "title": title,
                    "summary": (
                        f"Polymarket '{question}' YES price {direction} from "
                        f"{old_price:.1%} to {yes_price:.1%} ({price_change:+.1%}) in {lookback_label}. "
                        f"Volume: ${volume:,.0f}. "
                        f"Rapid price movement may indicate insider information or breaking news."
                    ),
                    "published": now_iso,
                    "fetched_at": now_iso,
                    "url": f"https://polymarket.com/market/{mid}",
                    "meta": {
                        "market_id": mid,
                        "old_price": old_price,
                        "new_price": yes_price,
                        "change": price_change,
                        "change_pct": pct_change,
                        "lookback": lookback_label,
                    },
                })

        # Volume spike detection
        if len(entries) >= 5:
            recent_volumes = [e.get("v", 0) for e in entries[-6:-1]]
            avg_vol = sum(recent_volumes) / max(1, len(recent_volumes))
            if avg_vol > 0 and volume > avg_vol * VOLUME_SPIKE_MULT:
                title = f"📊 Volume Spike: {question[:50]} — {volume/avg_vol:.1f}x average"
                alerts.append({
                    "id": _item_id(f"vol-{mid}-{now.strftime('%H%M')}"),
                    "source": "Volume-Spike",
                    "title": title,
                    "summary": (
                        f"Polymarket '{question}' volume spiked to ${volume:,.0f} "
                        f"({volume/avg_vol:.1f}x recent average of ${avg_vol:,.0f})."
                    ),
                    "published": now_iso,
                    "fetched_at": now_iso,
                    "url": f"https://polymarket.com/market/{mid}",
                })

    save_price_history(history)
    return alerts


if __name__ == "__main__":
    from .market_cache import get_markets
    from rich.console import Console
    console = Console()

    console.print("[bold]Loading markets and checking for anomalies...[/bold]")
    markets = get_markets()
    alerts = record_and_detect(markets)
    console.print(f"[green]{len(alerts)} anomalies detected[/green]")
    for a in alerts:
        console.print(f"  [{a['source']}] {a['title']}")

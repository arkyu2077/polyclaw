"""Polymarket volume monitor — detect unusual volume spikes (smart money signal).
Compares 24h volume to historical average to find markets with sudden interest.
"""
import json
import time
from datetime import datetime, timezone

import httpx

from .config import get_config

GAMMA_API = "https://gamma-api.polymarket.com"

# Minimum 24h volume to be worth tracking
MIN_VOLUME_24H = 50_000
# Volume spike threshold: 24h vol > X% of total volume = unusual
SPIKE_RATIO = 0.25
# Minimum absolute 24h volume to alert
MIN_SPIKE_VOLUME = 100_000


def _get_state_file():
    config = get_config()
    return config._data_path / "volume_state.json"


def _load_state():
    try:
        return json.loads(_get_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "prev_volumes": {}, "alerted": {}}


def _save_state(state):
    now = time.time()
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items() if now - v < 86400}
    _get_state_file().write_text(json.dumps(state))


def detect_volume_spikes(top_n: int = 50) -> list[dict]:
    """Fetch top markets by 24h volume, detect unusual spikes."""
    state = _load_state()

    if time.time() - state.get("last_fetch", 0) < 300:
        return []

    try:
        resp = httpx.get(
            f"{GAMMA_API}/markets",
            params={
                "limit": top_n,
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception:
        return []

    state["last_fetch"] = time.time()
    alerts = []
    prev_vols = state.get("prev_volumes", {})
    alerted = state.get("alerted", {})

    for m in markets:
        try:
            mid = m.get("id", "")
            question = m.get("question", "")
            vol_24h = float(m.get("volume24hr", 0) or 0)
            vol_total = float(m.get("volume", 0) or 0)

            if vol_24h < MIN_VOLUME_24H:
                continue

            spike_reasons = []

            if vol_total > 0:
                ratio = vol_24h / vol_total
                if ratio >= SPIKE_RATIO and vol_24h >= MIN_SPIKE_VOLUME:
                    spike_reasons.append(f"{ratio:.0%} of all-time volume in 24h")

            prev = prev_vols.get(mid, 0)
            if prev > 0 and vol_24h > prev * 2 and vol_24h >= MIN_SPIKE_VOLUME:
                spike_reasons.append(f"volume {vol_24h/prev:.1f}x vs last check")

            prev_vols[mid] = vol_24h

            if not spike_reasons:
                continue

            if mid in alerted and time.time() - alerted[mid] < 21600:
                continue

            alerted[mid] = time.time()

            prices_str = m.get("outcomePrices", "[]")
            try:
                prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                yes_price = float(prices[0]) if prices else 0
            except Exception:
                yes_price = 0

            importance = 5 if vol_24h > 1_000_000 else 4

            summary = (
                f"[VOLUME SPIKE] {question} — "
                f"24h vol: ${vol_24h:,.0f} | {', '.join(spike_reasons)} | "
                f"YES: {yes_price:.1%}"
            )

            alerts.append({
                "title": summary[:150],
                "summary": summary,
                "source": "polymarket_volume",
                "source_detail": f"Polymarket (24h vol ${vol_24h:,.0f})",
                "published": datetime.now(timezone.utc).isoformat(),
                "url": f"https://polymarket.com/event/{m.get('conditionId', mid)}",
                "importance": importance,
                "_market_id": mid,
                "_volume_24h": vol_24h,
                "_yes_price": yes_price,
            })

        except Exception:
            continue

    state["prev_volumes"] = prev_vols
    state["alerted"] = alerted
    _save_state(state)
    return alerts


if __name__ == "__main__":
    alerts = detect_volume_spikes()
    print(f"Volume spikes: {len(alerts)}")
    for a in alerts:
        print(f"  [{a['importance']}] {a['title']}")

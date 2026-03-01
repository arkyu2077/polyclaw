"""EIA energy data source — US crude oil prices. Requires free API key."""
import json
import hashlib
import time
from datetime import datetime, timezone

import httpx

from .config import get_config

MIN_INTERVAL = 3600  # 1 hour

# WTI and Brent spot price series
SERIES = {
    "RWTC": "WTI Crude",
    "RBRTE": "Brent Crude",
}


def _state_file():
    return get_config()._data_path / "eia_state.json"


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "last_prices": {}}


def _save_state(state):
    _state_file().write_text(json.dumps(state))


def _change_importance(pct_change: float) -> int:
    abs_change = abs(pct_change)
    if abs_change > 5:
        return 5
    if abs_change > 3:
        return 4
    if abs_change > 1:
        return 3
    return 2


def fetch_eia() -> list[dict]:
    """Fetch crude oil prices from EIA API. Requires EIA_API_KEY."""
    cfg = get_config()
    api_key = cfg.eia_api_key
    if not api_key:
        return []  # Silent skip

    state = _load_state()
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    last_prices = state.get("last_prices", {})
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    for series_id, label in SERIES.items():
        try:
            resp = httpx.get(
                "https://api.eia.gov/v2/petroleum/pri/spt/data/",
                params={
                    "api_key": api_key,
                    "frequency": "daily",
                    "data[0]": "value",
                    "facets[series][]": series_id,
                    "sort[0][column]": "period",
                    "sort[0][direction]": "desc",
                    "length": "2",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            records = data.get("response", {}).get("data", [])
            if not records:
                continue

            current = records[0]
            price = float(current.get("value", 0))
            period = current.get("period", "")

            if not price:
                continue

            # Calculate change from previous
            prev_price = last_prices.get(series_id, 0)
            if prev_price > 0:
                pct_change = ((price - prev_price) / prev_price) * 100
                sign = "+" if pct_change >= 0 else ""
                title = f"{label}: ${price:.2f}/bbl ({sign}{pct_change:.1f}%)"
                importance = _change_importance(pct_change)
            else:
                title = f"{label}: ${price:.2f}/bbl"
                importance = 2

            last_prices[series_id] = price

            items.append({
                "id": f"eia-{hashlib.sha256(f'{series_id}:{period}'.encode()).hexdigest()[:16]}",
                "title": title,
                "summary": f"{label} spot price for {period}",
                "source": "eia",
                "published": ts,
                "fetched_at": ts,
                "url": "https://www.eia.gov/petroleum/",
                "importance": importance,
            })
        except Exception:
            continue

    state["last_fetch"] = now
    state["last_prices"] = last_prices
    _save_state(state)
    return items

"""Weather source — Open-Meteo free API. Tracks temps for Polymarket temperature markets."""
import httpx
import json
import os
import time
from datetime import datetime, timezone

CITIES = {
    "NYC": (40.71, -74.01),
    "LA": (34.05, -118.24),
    "Chicago": (41.88, -87.63),
    "Miami": (25.76, -80.19),
    "London": (51.51, -0.13),
}
THRESHOLDS_F = [32, 50, 60, 70, 80, 90, 100]
THRESHOLD_MARGIN = 3  # °F
STATE_FILE = os.path.join(os.path.dirname(__file__), "weather_state.json")
MIN_INTERVAL = 1800  # 30 minutes


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _near_threshold(temp_f: float) -> bool:
    return any(abs(temp_f - t) <= THRESHOLD_MARGIN for t in THRESHOLDS_F)


def _load_state(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"last_fetch": 0}


def _save_state(state, state_file):
    with open(state_file, "w") as f:
        json.dump(state, f)


def fetch_weather(state_file: str = STATE_FILE) -> list[dict]:
    """Fetch weather for key cities. Returns news-format dicts."""
    state = _load_state(state_file)
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    items = []
    ts = datetime.now(timezone.utc).isoformat()

    for city, (lat, lon) in CITIES.items():
        try:
            resp = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,precipitation",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "America/New_York",
                    "forecast_days": 3,
                },
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()

            current_c = data.get("current", {}).get("temperature_2m", 0)
            current_f = _c_to_f(current_c)
            daily = data.get("daily", {})
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])

            high_f = _c_to_f(highs[0]) if highs else current_f
            low_f = _c_to_f(lows[0]) if lows else current_f

            title = f"{city}: {current_f:.0f}°F ({current_c:.0f}°C) now, high {high_f:.0f}°F / low {low_f:.0f}°F"

            near = _near_threshold(high_f) or _near_threshold(current_f)
            if near:
                # Find nearest threshold
                nearest = min(THRESHOLDS_F, key=lambda t: abs(high_f - t))
                title += f" ⚠️ Near {nearest}°F threshold!"

            items.append({
                "title": title,
                "source": f"weather:{city}",
                "importance": 3 if near else 1,
                "timestamp": ts,
            })
        except Exception:
            continue

    state["last_fetch"] = now
    _save_state(state, state_file)
    return items


if __name__ == "__main__":
    results = fetch_weather()
    for r in results:
        print(f"[{r['importance']}] {r['source']}: {r['title']}")
    print(f"\nTotal: {len(results)} items")

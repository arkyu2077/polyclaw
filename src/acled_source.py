"""ACLED armed conflict data source — requires free account (OAuth token auth)."""
import json
import hashlib
import time
from datetime import datetime, timezone, timedelta

import httpx

from .config import get_config

MIN_INTERVAL = 3600  # 1 hour
TOKEN_URL = "https://acleddata.com/oauth/token"
API_URL = "https://api.acleddata.com/acled/read"


def _state_file():
    return get_config()._data_path / "acled_state.json"


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"last_fetch": 0, "seen_ids": [], "access_token": "", "token_expires": 0}


def _save_state(state):
    state["seen_ids"] = state.get("seen_ids", [])[-300:]
    _state_file().write_text(json.dumps(state))


def _fatalities_importance(fatalities: int) -> int:
    if fatalities >= 50:
        return 5
    if fatalities >= 10:
        return 4
    if fatalities >= 1:
        return 3
    return 2


def _get_access_token(email: str, password: str, state: dict) -> str:
    """Get or refresh OAuth access token. Tokens are valid for 24 hours."""
    now = time.time()
    cached_token = state.get("access_token", "")
    token_expires = state.get("token_expires", 0)

    # Reuse cached token if still valid (with 1h safety margin)
    if cached_token and now < token_expires - 3600:
        return cached_token

    resp = httpx.post(
        TOKEN_URL,
        data={
            "username": email,
            "password": password,
            "grant_type": "password",
            "client_id": "acled",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return ""

    data = resp.json()
    token = data.get("access_token", "")
    expires_in = int(data.get("expires_in", 86400))  # default 24h

    state["access_token"] = token
    state["token_expires"] = now + expires_in
    return token


def fetch_acled() -> list[dict]:
    """Fetch conflict events from ACLED. Requires ACLED_EMAIL + ACLED_PASSWORD."""
    cfg = get_config()
    email = cfg.acled_email
    password = cfg.acled_password
    if not email or not password:
        return []  # Silent skip if no credentials

    state = _load_state()
    now = time.time()

    if now - state.get("last_fetch", 0) < MIN_INTERVAL:
        return []

    # Get OAuth token
    token = _get_access_token(email, password, state)
    if not token:
        _save_state(state)
        return []

    seen = set(state.get("seen_ids", []))
    items = []
    ts = datetime.now(timezone.utc).isoformat()

    # Query last 2 days
    today = datetime.now(timezone.utc)
    date_from = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    try:
        resp = httpx.get(
            API_URL,
            params={
                "event_date": f"{date_from}|{date_to}",
                "event_date_where": "BETWEEN",
                "limit": "50",
                "fields": "event_id_cnty|event_date|event_type|sub_event_type|country|location|fatalities|source",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        events = data.get("data", [])

        # Sort by fatalities descending
        events.sort(key=lambda e: int(e.get("fatalities", 0)), reverse=True)

        for event in events:
            eid = event.get("event_id_cnty", "")
            if not eid or eid in seen:
                continue
            seen.add(eid)

            fatalities = int(event.get("fatalities", 0))
            event_type = event.get("event_type", "Event")
            location = event.get("location", "Unknown")
            country = event.get("country", "Unknown")

            fat_str = f" — {fatalities} fatalities" if fatalities > 0 else ""
            title = f"{event_type} in {location}, {country}{fat_str}"

            items.append({
                "id": f"acled-{hashlib.sha256(eid.encode()).hexdigest()[:16]}",
                "title": title,
                "summary": f"{event.get('sub_event_type', '')} on {event.get('event_date', '')}",
                "source": "acled",
                "published": event.get("event_date", ts),
                "fetched_at": ts,
                "url": "https://acleddata.com/",
                "importance": _fatalities_importance(fatalities),
            })
    except Exception:
        pass

    state["last_fetch"] = now
    state["seen_ids"] = list(seen)
    _save_state(state)
    return items

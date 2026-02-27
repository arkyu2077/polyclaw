"""Polymarket market cache via Gamma API."""

import json
import time
from pathlib import Path
from datetime import datetime, timezone

import httpx

DATA_DIR = Path(__file__).parent
CACHE_FILE = DATA_DIR / "market_cache.json"
CACHE_TTL = 300  # 5 minutes

GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def fetch_markets() -> list[dict]:
    """Fetch active markets from Gamma API, return parsed list."""
    # Fetch multiple pages: high-volume + newest (more likely to have edges)
    raw = []
    for order, limit in [("volume", 100), ("startDate", 100), ("liquidity", 100)]:
        try:
            params = {"closed": "false", "limit": limit, "order": order, "ascending": "false"}
            resp = httpx.get(GAMMA_URL, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            raw.extend(resp.json())
        except Exception:
            pass
    # Deduplicate by id
    seen = set()
    deduped = []
    for m in raw:
        mid = m.get("id", "")
        if mid not in seen:
            seen.add(mid)
            deduped.append(m)
    raw = deduped

    markets = []
    for m in raw:
        try:
            outcome_prices = json.loads(m.get("outcomePrices", "[]"))
            prices = [float(p) for p in outcome_prices]
        except (json.JSONDecodeError, ValueError):
            prices = []

        try:
            clob_ids = json.loads(m.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, ValueError):
            clob_ids = []

        markets.append({
            "id": m.get("id", ""),
            "condition_id": m.get("conditionId", m.get("condition_id", "")),
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "outcomePrices": prices,
            "outcomes": m.get("outcomes", ""),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "endDate": m.get("endDate", ""),
            "clobTokenIds": clob_ids,
            "neg_risk": bool(m.get("negRisk") or m.get("neg_risk")),
            "fee_type": m.get("feeType", ""),
            "description": m.get("description", "")[:300],
        })
    return markets


def get_markets(force_refresh: bool = False) -> list[dict]:
    """Return cached markets, refreshing if stale."""
    if not force_refresh and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data["markets"]
        except Exception:
            pass

    markets = fetch_markets()
    CACHE_FILE.write_text(json.dumps({
        "fetched_at": time.time(),
        "count": len(markets),
        "markets": markets,
    }, indent=2))
    return markets


if __name__ == "__main__":
    from rich.console import Console
    console = Console()
    console.print("[bold]Fetching Polymarket markets...[/bold]")
    mkts = get_markets(force_refresh=True)
    console.print(f"[green]{len(mkts)} markets cached[/green]")
    for m in mkts[:5]:
        console.print(f"  {m['question'][:80]}  prices={m['outcomePrices']}")

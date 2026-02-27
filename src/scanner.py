#!/usr/bin/env python3
"""Polymarket News Edge Scanner — main orchestrator."""

import json
import os
import sys
import time
import signal
import argparse
import traceback
import fcntl
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from news_ingestion import ingest
from market_cache import get_markets
from event_parser import parse_all, parse_with_llm
from probability_engine import compute_estimates, merge_llm_estimates
from edge_calculator import find_edges, TradeSignal
from position_manager import open_position, check_exits, display_positions, _fetch_market_price
from price_monitor import record_and_detect
from strategy_arena import run_arena, check_arena_exits
from db import (
    insert_signal, get_cooldown, set_cooldown, prune_cooldowns,
    get_all_cooldowns,
)

console = Console()

SIGNAL_COOLDOWN_HOURS = 4  # Don't re-alert same market+direction within this window
MAX_ALERTS_PER_HOUR = 5     # Discord rate limit


def _log_dedup(sig, age_hours: float):
    """Log a signal filtered by cooldown dedup — stored in signals table."""
    insert_signal({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_id": sig.market_id,
        "question": sig.question[:80],
        "direction": sig.direction,
        "current_price": getattr(sig, "current_price", 0),
        "ai_probability": getattr(sig, "ai_probability", 0),
        "edge": round(sig.edge, 4) if hasattr(sig, 'edge') else 0,
        "raw_edge": getattr(sig, "raw_edge", 0),
        "fee_estimate": getattr(sig, "fee_estimate", 0),
        "confidence": getattr(sig, "confidence", 0),
        "position_size": getattr(sig, "position_size", 0),
        "reliability": getattr(sig, "reliability", ""),
        "news_titles": getattr(sig, "signals", {}).get("news_titles", []),
        "llm_reasoning": getattr(sig, "signals", {}).get("llm_reasoning", ""),
        "filter_reason": "cooldown_dedup",
        "cooldown_age_hours": round(age_hours, 2),
    })


def dedup_signals(signals: list) -> list:
    """Filter out signals that were already alerted within cooldown window."""
    now = datetime.now(timezone.utc)
    fresh = []

    for sig in signals:
        key = f"{sig.market_id}::{sig.direction}"
        last_alert = get_cooldown(key)
        if last_alert:
            try:
                last_dt = datetime.fromisoformat(last_alert)
                age_hours = (now - last_dt).total_seconds() / 3600
                if age_hours < SIGNAL_COOLDOWN_HOURS:
                    _log_dedup(sig, age_hours)
                    continue  # Still in cooldown
            except Exception:
                pass
        fresh.append(sig)
        set_cooldown(key, now.isoformat())

    # Prune cooldowns older than 24h
    cutoff = now.timestamp() - 86400
    prune_cooldowns(cutoff)

    # Rate limit: max N per hour
    if len(fresh) > MAX_ALERTS_PER_HOUR:
        fresh = sorted(fresh, key=lambda s: abs(s.edge), reverse=True)[:MAX_ALERTS_PER_HOUR]

    return fresh


def run_scan(min_edge: float = 0.03, bankroll: float = 1000.0, use_llm: bool = False, llm_only: bool = False) -> list[TradeSignal]:
    """Execute a single scan cycle."""
    # 1. Ingest news (RSS + Twitter)
    console.print("\n[bold cyan]📰 Fetching news feeds...[/bold cyan]")
    new_items = ingest()
    # Twitter/X via RapidAPI
    try:
        from twitter_source import fetch_all as fetch_tweets
        tweets = fetch_tweets()
        if tweets:
            new_items.extend(tweets)
            console.print(f"  🐦 {len(tweets)} tweets fetched")
    except Exception as e:
        console.print(f"  [dim]Twitter: {e}[/dim]")
    # Economic calendar
    try:
        from economic_calendar import fetch_calendar
        econ = fetch_calendar()
        if econ:
            new_items.extend(econ)
            console.print(f"  📅 {len(econ)} upcoming economic events")
    except Exception as e:
        console.print(f"  [dim]EconCal: {e}[/dim]")
    # Polymarket volume spikes
    try:
        from volume_monitor import detect_volume_spikes
        spikes = detect_volume_spikes()
        if spikes:
            new_items.extend(spikes)
            console.print(f"  🔊 {len(spikes)} volume spikes detected!")
    except Exception as e:
        console.print(f"  [dim]VolMon: {e}[/dim]")
    # Reddit
    try:
        from reddit_source import fetch_reddit
        reddit_items = fetch_reddit()
        if reddit_items:
            new_items.extend(reddit_items)
            console.print(f"  🔴 {len(reddit_items)} Reddit posts fetched")
    except Exception as e:
        console.print(f"  [dim]Reddit: {e}[/dim]")
    # Weather
    try:
        from weather_source import fetch_weather
        weather_items = fetch_weather()
        if weather_items:
            new_items.extend(weather_items)
            console.print(f"  🌡️ {len(weather_items)} weather updates")
    except Exception as e:
        console.print(f"  [dim]Weather: {e}[/dim]")
    # Sports Odds
    try:
        from sports_odds import fetch_sports_odds
        odds_items = fetch_sports_odds()
        if odds_items:
            new_items.extend(odds_items)
            console.print(f"  🏀 {len(odds_items)} sports odds fetched")
    except Exception as e:
        console.print(f"  [dim]Sports odds: {e}[/dim]")
    news_file = Path(__file__).parent / "news_feed.json"
    all_news = json.loads(news_file.read_text()) if news_file.exists() else []
    console.print(f"  {len(new_items)} new items, {len(all_news)} total in cache")

    # 2. Fetch markets
    console.print("[bold cyan]📊 Loading Polymarket markets...[/bold cyan]")
    markets = get_markets()
    console.print(f"  {len(markets)} active markets loaded")

    # 2b. Price anomaly detection
    console.print("[bold cyan]📈 Checking price anomalies...[/bold cyan]")
    try:
        price_alerts = record_and_detect(markets)
        if price_alerts:
            console.print(f"  [bold red]🚨 {len(price_alerts)} price anomalies detected![/bold red]")
            for pa in price_alerts:
                console.print(f"    [red]→[/red] {pa['title']}")
            all_news = price_alerts + all_news
        else:
            console.print(f"  [dim]No anomalies[/dim]")
    except Exception as e:
        console.print(f"  [dim red]Price monitor error: {e}[/dim red]")

    # 3. Parse news → signals (with dedup and category matching)
    console.print("[bold cyan]🔍 Analyzing news against markets...[/bold cyan]")
    if not llm_only:
        signals = parse_all(all_news, markets)
        console.print(f"  {len(signals)} news items matched to markets (keywords)")
    else:
        signals = []
        console.print("  [dim]Keyword matching skipped (--llm-only)[/dim]")

    # 4. Compute probabilities (aggregated per market)
    console.print("[bold cyan]🧮 Computing probability estimates...[/bold cyan]")
    markets_by_id = {m["id"]: m for m in markets}
    estimates = compute_estimates(signals, markets_by_id) if signals else []
    console.print(f"  {len(estimates)} keyword estimates generated")

    # 4b. LLM analysis
    llm_signals = []
    if use_llm or llm_only:
        console.print("[bold magenta]🤖 Running Gemini LLM analysis...[/bold magenta]")
        llm_signals = parse_with_llm(all_news, markets)
        console.print(f"  {len(llm_signals)} LLM signals found")
        if llm_signals:
            for s in llm_signals[:5]:
                console.print(f"    [magenta]→[/magenta] {s['news_title'][:50]} ⟶ {s['question'][:40]} ({s['direction']}, p={s['estimated_probability']:.0%})")
                if s.get('reasoning'):
                    console.print(f"      [dim]{s['reasoning'][:80]}[/dim]")
            estimates = merge_llm_estimates(estimates, llm_signals)

    # 5. Find edges (with fee adjustment)
    console.print("[bold cyan]⚡ Scanning for edges (fee-adjusted)...[/bold cyan]")
    trade_signals = find_edges(estimates, bankroll=bankroll, min_edge=min_edge)

    # Display results
    display_results(trade_signals, estimates, all_news, markets)

    # Dedup: filter out signals already alerted in cooldown window
    fresh_signals = dedup_signals(trade_signals)
    if len(trade_signals) != len(fresh_signals):
        console.print(f"  [dim]Dedup: {len(trade_signals)} signals → {len(fresh_signals)} fresh (cooldown={SIGNAL_COOLDOWN_HOURS}h)[/dim]")

    save_signals(fresh_signals)

    # Position management — only open positions for fresh signals
    for sig in fresh_signals:
        # Prefer LLM reasoning over keyword-matched news title
        llm_reasoning = sig.signals.get("llm_reasoning", "")
        titles = sig.signals.get("news_titles", [])
        if llm_reasoning:
            trigger = f"[LLM] {llm_reasoning[:100]}"
        elif titles:
            trigger = titles[0]
        else:
            trigger = ""
        open_position(
            market_id=sig.market_id,
            question=sig.question,
            direction=sig.direction,
            entry_price=sig.current_price if sig.direction == "BUY_YES" else (1 - sig.current_price),
            ai_probability=sig.ai_probability if sig.direction == "BUY_YES" else (1 - sig.ai_probability),
            bankroll=bankroll,
            trigger_news=trigger,
            confidence=sig.confidence,
        )

    # Check exits on open positions
    console.print("[bold cyan]📋 Checking open positions...[/bold cyan]")
    closed = check_exits()
    if closed:
        console.print(f"  Closed {closed} position(s) this cycle")

    # Display position summary
    display_positions(bankroll)

    # --- LIVE TRADING: check exits + cleanup + auto-redeem ---
    console.print("[bold red]💰 LIVE TRADING: monitoring...[/bold red]")
    try:
        from live_trader import check_live_exits, auto_redeem_resolved, get_live_positions, cleanup_stale_orders
        live_closed = check_live_exits()
        if live_closed:
            console.print(f"  [bold yellow]📤 Closed {live_closed} live positions[/bold yellow]")
        live_open = len([p for p in get_live_positions() if p.get("status") == "open"])
        console.print(f"  Live positions: {live_open} open")

        # Cleanup stale orders (>12h, expiring, price drifted)
        try:
            stale = cleanup_stale_orders()
            if stale:
                console.print(f"  [yellow]🗑️ Cancelled {stale} stale orders[/yellow]")
        except Exception as cleanup_err:
            console.print(f"  [yellow]⚠ Order cleanup: {cleanup_err}[/yellow]")

        # Auto-redeem resolved markets every cycle
        try:
            redeemed = auto_redeem_resolved()
            if redeemed:
                console.print(f"  [bold green]🔄 Redeemed {redeemed} resolved positions → USDC.e back to wallet[/bold green]")
        except Exception as redeem_err:
            console.print(f"  [yellow]⚠ Redeem check: {redeem_err}[/yellow]")
    except Exception as e:
        console.print(f"  [red]Live trading error: {e}[/red]")

    # --- Strategy Arena: run all variants on same estimates ---
    console.print("[bold cyan]🏟️  Strategy Arena: feeding estimates to all variants...[/bold cyan]")
    try:
        run_arena(estimates, bankroll, live_trading=True)
        arena_closed = check_arena_exits(_fetch_market_price)

        # Count open arena positions from db
        from db import get_positions as db_get_positions
        arena_positions = db_get_positions(mode="arena", status="open")
        arena_opens = len(arena_positions)
        console.print(f"  Arena: {arena_opens} open positions, closed {arena_closed} this cycle")
    except Exception as e:
        console.print(f"  [dim red]Arena error: {e}[/dim red]")
        import traceback
        traceback.print_exc()

    return trade_signals


def edge_color(edge: float) -> str:
    if edge >= 0.08:
        return "bold green"
    elif edge >= 0.05:
        return "green"
    elif edge >= 0.03:
        return "yellow"
    return "white"


def reliability_icon(r: str) -> str:
    return {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(r, "⚪")


def display_results(trade_signals: list[TradeSignal], estimates, news, markets):
    """Display scan results grouped by market with color coding."""
    console.print()

    summary = (
        f"📰 News: {len(news)}  |  "
        f"📊 Markets: {len(markets)}  |  "
        f"🧮 Estimates: {len(estimates)}  |  "
        f"⚡ Signals: {len(trade_signals)}"
    )
    console.print(Panel(summary, title="[bold]Scan Summary[/bold]", border_style="green"))

    if not trade_signals:
        console.print("\n[yellow]No actionable edges found after fee adjustment.[/yellow]")
        console.print("[dim]This is expected — real edges are rare in liquid markets.[/dim]\n")

        if estimates:
            table = Table(title="Top Estimates (no edge after fees)", box=box.SIMPLE)
            table.add_column("Market", max_width=55)
            table.add_column("Price", justify="right")
            table.add_column("AI Est.", justify="right")
            table.add_column("Raw Δ", justify="right")
            table.add_column("#News", justify="right")
            table.add_column("Trigger", max_width=40)

            for est in sorted(estimates, key=lambda e: abs(e.ai_probability - e.current_price), reverse=True)[:8]:
                diff = est.ai_probability - est.current_price
                dc = "green" if diff > 0 else "red" if diff < 0 else "white"
                titles = est.signals.get("news_titles", [])
                trigger = titles[0][:40] if titles else ""
                table.add_row(
                    est.question[:55],
                    f"{est.current_price:.1%}",
                    f"{est.ai_probability:.1%}",
                    f"[{dc}]{diff:+.1%}[/{dc}]",
                    str(est.signals.get("n_signals", 0)),
                    trigger,
                )
            console.print(table)
        return

    # Trade signals table — grouped, color-coded
    table = Table(title="🚨 Trade Signals (fee-adjusted)", box=box.DOUBLE_EDGE, border_style="bold green")
    table.add_column("Rel", justify="center", width=3)
    table.add_column("Market", max_width=45)
    table.add_column("Dir", justify="center")
    table.add_column("Mkt", justify="right")
    table.add_column("AI", justify="right")
    table.add_column("Raw Δ", justify="right")
    table.add_column("Fee", justify="right")
    table.add_column("Edge", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("#N", justify="right")
    table.add_column("Size$", justify="right")
    table.add_column("Trigger", max_width=30)

    for sig in trade_signals:
        ec = edge_color(sig.edge)
        titles = sig.signals.get("news_titles", [])
        trigger = titles[0][:30] if titles else ""
        table.add_row(
            reliability_icon(sig.reliability),
            sig.question[:45],
            f"[{'green' if 'YES' in sig.direction else 'red'}]{sig.direction}[/]",
            f"{sig.current_price:.1%}",
            f"{sig.ai_probability:.1%}",
            f"{sig.raw_edge:+.1%}",
            f"{sig.fee_estimate:.1%}",
            f"[{ec}]{sig.edge:+.1%}[/{ec}]",
            f"{sig.confidence:.0%}",
            str(sig.signals.get("n_signals", 0)),
            f"${sig.position_size:.0f}",
            trigger,
        )
    console.print(table)


def save_signals(trade_signals: list[TradeSignal]):
    """Save signals to db and write ALERT.json for external cron."""
    now = datetime.now(timezone.utc).isoformat()

    for sig in trade_signals:
        d = asdict(sig)
        insert_signal({
            "timestamp": now,
            "market_id": d.get("market_id"),
            "question": d.get("question"),
            "direction": d.get("direction"),
            "current_price": d.get("current_price"),
            "ai_probability": d.get("ai_probability"),
            "edge": d.get("edge"),
            "raw_edge": d.get("raw_edge"),
            "fee_estimate": d.get("fee_estimate"),
            "confidence": d.get("confidence"),
            "position_size": d.get("position_size"),
            "reliability": d.get("reliability"),
            "news_titles": d.get("signals", {}).get("news_titles", []),
            "llm_reasoning": d.get("signals", {}).get("llm_reasoning", ""),
            "filter_reason": None,
            "cooldown_age_hours": None,
        })

    # Write alert file when signals found — cron job picks this up (keep as JSON)
    alert_file = Path(__file__).parent / "ALERT.json"
    if trade_signals:
        alert = {
            "timestamp": now,
            "count": len(trade_signals),
            "top_signals": [
                {
                    "market": s.question[:60],
                    "direction": s.direction,
                    "edge": f"{s.edge:+.1%}",
                    "price": f"{s.current_price:.1%}",
                    "ai": f"{s.ai_probability:.1%}",
                }
                for s in trade_signals[:5]
            ],
        }
        alert_file.write_text(json.dumps(alert, indent=2, ensure_ascii=False))
    elif alert_file.exists():
        alert_file.unlink()


def main():
    parser = argparse.ArgumentParser(description="Polymarket News Edge Scanner")
    parser.add_argument("--scan", action="store_true", help="One-shot scan")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Min edge after fees (default: 0.03)")
    parser.add_argument("--bankroll", type=float, default=1000.0, help="Paper bankroll (default: $1000)")
    parser.add_argument("--interval", type=int, default=60, help="Monitor interval seconds")
    parser.add_argument("--use-llm", action="store_true", help="Enable Gemini LLM analysis (in addition to keywords)")
    parser.add_argument("--llm-only", action="store_true", help="Use only LLM analysis (skip keyword matching)")
    parser.add_argument("--positions", action="store_true", help="Show current positions and exit")
    args = parser.parse_args()

    if args.positions:
        display_positions(args.bankroll)
        return

    console.print(Panel(
        "[bold]Polymarket News Edge Scanner v2[/bold]\n"
        "Category-aware matching · Directional probability · Fee-adjusted edges\n"
        + ("[magenta]🤖 Gemini LLM enabled[/magenta]\n" if (args.use_llm or args.llm_only) else "")
        + "[dim]Paper trading only — no real trades[/dim]",
        border_style="blue",
    ))

    if args.monitor:
        console.print(f"[bold]Monitoring[/bold] every {args.interval}s (Ctrl+C to stop)\n")
        consecutive_errors = 0
        max_consecutive_errors = 10

        # ── Single-instance lock (prevents duplicate processes) ──
        lock_file = Path(__file__).parent / "scanner.lock"
        lock_fd = open(lock_file, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            console.print("[red bold]Another scanner instance is already running. Exiting.[/red bold]")
            sys.exit(1)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()

        # Write PID file for watchdog
        pid_file = Path(__file__).parent / "scanner.pid"
        pid_file.write_text(str(os.getpid()))

        # Heartbeat file — watchdog checks this
        heartbeat_file = Path(__file__).parent / "scanner_heartbeat"

        try:
            while True:
                try:
                    heartbeat_file.write_text(datetime.now(timezone.utc).isoformat())

                    scan_timeout = max(args.interval * 3, 180)
                    def _timeout_handler(signum, frame):
                        raise TimeoutError(f"Scan exceeded {scan_timeout}s timeout")
                    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                    signal.alarm(scan_timeout)

                    run_scan(min_edge=args.min_edge, bankroll=args.bankroll, use_llm=args.use_llm, llm_only=args.llm_only)

                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
                    consecutive_errors = 0

                except TimeoutError as e:
                    consecutive_errors += 1
                    console.print(f"\n[red]⏰ Scan timeout: {e}[/red] ({consecutive_errors}/{max_consecutive_errors})")
                    signal.alarm(0)
                except Exception as e:
                    consecutive_errors += 1
                    console.print(f"\n[red]Scan error ({consecutive_errors}/{max_consecutive_errors}): {e}[/red]")
                    traceback.print_exc()

                if consecutive_errors >= max_consecutive_errors:
                    console.print(f"\n[bold red]💀 {max_consecutive_errors} consecutive errors — exiting for restart[/bold red]")
                    break

                console.print(f"\n[dim]Next scan in {args.interval}s...[/dim]")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
        finally:
            pid_file.unlink(missing_ok=True)
    else:
        run_scan(min_edge=args.min_edge, bankroll=args.bankroll, use_llm=args.use_llm, llm_only=args.llm_only)


if __name__ == "__main__":
    main()

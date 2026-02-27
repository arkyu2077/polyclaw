#!/usr/bin/env python3
"""Polymarket News Edge Scanner — main orchestrator."""

import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from .news_ingestion import ingest
from .market_cache import get_markets
from .event_parser import parse_all, parse_with_llm
from .probability_engine import compute_estimates, merge_llm_estimates
from .edge_calculator import find_edges, TradeSignal
from .position_manager import open_position, check_exits, display_positions

console = Console()
SIGNALS_LOG = Path(__file__).parent / "signals_log.json"


def run_scan(min_edge: float = 0.03, bankroll: float = 1000.0, use_llm: bool = False, llm_only: bool = False) -> list[TradeSignal]:
    """Execute a single scan cycle."""
    # 1. Ingest news
    console.print("\n[bold cyan]📰 Fetching news feeds...[/bold cyan]")
    new_items = ingest()
    news_file = Path(__file__).parent / "news_feed.json"
    all_news = json.loads(news_file.read_text()) if news_file.exists() else []
    console.print(f"  {len(new_items)} new items, {len(all_news)} total in cache")

    # 2. Fetch markets
    console.print("[bold cyan]📊 Loading Polymarket markets...[/bold cyan]")
    markets = get_markets()
    console.print(f"  {len(markets)} active markets loaded")

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
    save_signals(trade_signals)

    # Position management
    # Open positions for new signals (paper mode)
    for sig in trade_signals:
        titles = sig.signals.get("news_titles", [])
        trigger = titles[0] if titles else ""
        open_position(
            market_id=sig.market_id,
            question=sig.question,
            direction=sig.direction,
            entry_price=sig.current_price if sig.direction == "BUY_YES" else (1 - sig.current_price),
            ai_probability=sig.ai_probability if sig.direction == "BUY_YES" else (1 - sig.ai_probability),
            bankroll=bankroll,
            trigger_news=trigger,
        )

    # Check exits on open positions
    console.print("[bold cyan]📋 Checking open positions...[/bold cyan]")
    closed = check_exits()
    if closed:
        console.print(f"  Closed {closed} position(s) this cycle")

    # Display position summary
    display_positions(bankroll)

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
    existing = []
    if SIGNALS_LOG.exists():
        try:
            existing = json.loads(SIGNALS_LOG.read_text())
        except Exception:
            existing = []

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signals_count": len(trade_signals),
        "signals": [asdict(s) for s in trade_signals],
    }
    existing.append(entry)
    existing = existing[-100:]
    SIGNALS_LOG.write_text(json.dumps(existing, indent=2))

    # Write alert file when signals found — cron job picks this up
    alert_file = Path(__file__).parent / "ALERT.json"
    if trade_signals:
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        alert_file.unlink()  # Clear old alert


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
        try:
            while True:
                try:
                    run_scan(min_edge=args.min_edge, bankroll=args.bankroll, use_llm=args.use_llm, llm_only=args.llm_only)
                except Exception as e:
                    console.print(f"\n[red]Scan error: {e}[/red]")
                console.print(f"\n[dim]Next scan in {args.interval}s...[/dim]")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
    else:
        run_scan(min_edge=args.min_edge, bankroll=args.bankroll, use_llm=args.use_llm, llm_only=args.llm_only)


if __name__ == "__main__":
    main()

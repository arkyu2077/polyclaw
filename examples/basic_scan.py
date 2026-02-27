#!/usr/bin/env python3
"""Basic example: run a single scan and print results."""

from polymarket_news_edge import run_scan

if __name__ == "__main__":
    signals = run_scan(min_edge=0.03, bankroll=1000.0)
    print(f"\nFound {len(signals)} actionable signals")
    for s in signals:
        print(f"  {s.direction} {s.question[:60]}  edge={s.edge:+.1%}  size=${s.position_size:.0f}")

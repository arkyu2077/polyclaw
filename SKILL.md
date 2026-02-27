---
name: polyclaw
description: AI-powered news edge scanner and auto-trader for Polymarket. Ingests real-time news from 10+ sources, matches to Polymarket markets using category-aware fuzzy matching, estimates probability shifts, detects fee-adjusted trading edges, and auto-trades via CLOB API.
version: "1.1.0"
license: MIT
allowed-tools: Read,Write,Bash(python:*),WebFetch
---

# Polyclaw

## Prerequisites

- Python 3.10+
- pip packages: `feedparser httpx rich pandas rapidfuzz`
- **For LLM mode:** [Gemini CLI](https://github.com/google-gemini/gemini-cli) installed at `/opt/homebrew/bin/gemini`

## Quick Start

```bash
# Install dependencies
cd polyclaw/src
pip install -r requirements.txt

# Run a single scan
python scanner.py --scan

# Scan with Gemini LLM analysis
python scanner.py --scan --use-llm

# Pure LLM-only mode (skip rule-based matching)
python scanner.py --scan --llm-only

# View and manage open positions
python scanner.py --positions

# Run continuous monitoring
python scanner.py --monitor --interval 60

# Custom settings
python scanner.py --scan --min-edge 0.05 --bankroll 5000
```

## What's New (v1.1)

- **🤖 Gemini LLM Integration** — `--use-llm` merges LLM signals with rule-based analysis; `--llm-only` for pure LLM scanning
- **📈 Position Management** — Auto take-profit (+15%), stop-loss (-10%), 24h timeout with live price tracking
- **🇨🇳 Chinese Media Sources** — BlockBeats and PANews for crypto-native Chinese market intelligence
- **8 data sources total** — Reuters, AP, Bloomberg, CoinDesk, CoinGecko, Fear & Greed, BlockBeats, PANews

## Architecture

```
News Sources (8: RSS/APIs + Chinese media)
        │
        ▼
news_ingestion.py ──► Fetch + deduplicate + cache to news_feed.json
        │
        ▼
event_parser.py ◄──── market_cache.py (Polymarket Gamma API, 5-min TTL)
  │ Category-aware matching (crypto/politics/sports/economics/tech/geopolitics)
  │ Entity extraction (English + Chinese NLP)
  │ Negation-aware sentiment analysis
  │ Fuzzy matching with specificity gates
        │
        ▼
probability_engine.py ◄── llm_analyzer.py (optional, Gemini 2.5 Flash)
  │ Per-market signal aggregation
  │ LLM signal merging (when --use-llm)
  │ Directional shift logic (YES=up vs YES=down)
  │ Source credibility weighting
  │ Volume dampening for liquid markets
        │
        ▼
edge_calculator.py
  │ Polymarket fee schedule modeling
  │ Fee-adjusted edge computation
  │ Kelly criterion position sizing
        │
        ▼
position_manager.py
  │ Open/close paper positions
  │ Auto take-profit / stop-loss
  │ 24h timeout exit
        │
        ▼
scanner.py ──► Rich terminal UI with color-coded tables
```

## Module Descriptions

### `news_ingestion.py`
Fetches news from RSS feeds (Reuters, AP, Bloomberg, CoinDesk, The Block, Google News), CoinGecko trending, Fear & Greed Index, **BlockBeats**, and **PANews** (Chinese crypto media). Deduplicates by content hash. Rolling cache of 100 items.

### `market_cache.py`
Queries Polymarket Gamma API for top 100 active markets by volume. 5-minute cache TTL.

### `event_parser.py`
Category-aware matching with entity extraction (English + Chinese NLP), negation-aware sentiment, market question parsing, and LLM integration via `parse_with_llm()`.

### `probability_engine.py`
Multi-signal aggregation with directional logic, source weighting, volume dampening, and `merge_llm_estimates()` for combining rule-based and LLM signals.

### `edge_calculator.py`
Models Polymarket's taker fee schedule. Fee-adjusted edge for YES/NO sides. Kelly criterion sizing.

### `llm_analyzer.py`
Calls Gemini 2.5 Flash via CLI for structured news→market analysis. Batches up to 20 news items against all markets. Returns typed `LLMSignal` objects.

### `position_manager.py`
Tracks paper trading positions with:
- **Take-profit**: +15% from entry → auto close
- **Stop-loss**: -10% from entry → auto close
- **Timeout**: 24h with <2% move → close (signal expired)
- Max 5 open positions, 10% per position, 30% total exposure

### `scanner.py`
CLI orchestrator. Modes: `--scan`, `--monitor`, `--positions`. Flags: `--use-llm`, `--llm-only`.

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--min-edge` | 0.03 (3%) | Minimum edge after fees |
| `--bankroll` | $1,000 | Paper trading bankroll |
| `--interval` | 60s | Monitor scan interval |
| `--use-llm` | off | Add Gemini LLM analysis |
| `--llm-only` | off | Pure LLM mode |
| `--positions` | — | View/manage positions |

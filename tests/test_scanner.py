"""Tests for scanner.py — dedup_signals config-driven cooldown + LLM smart gate."""

import sys
import types
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from dataclasses import dataclass


# Pre-stub modules with broken/missing imports BEFORE importing scanner
def _stub(name, **attrs):
    """Stub a module only if it can't be imported normally."""
    if name in sys.modules:
        return sys.modules[name]
    try:
        __import__(name)
        return sys.modules[name]
    except Exception:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

_stub("news_ingestion", ingest=lambda: [])
_stub("market_cache", get_markets=lambda: [])
_stub("event_parser", parse_all=lambda *a, **kw: [], parse_with_llm=lambda *a, **kw: [])
_stub("probability_engine", compute_estimates=lambda *a, **kw: [], merge_llm_estimates=lambda *a, **kw: [])
_stub("edge_calculator", find_edges=lambda *a, **kw: [], TradeSignal=object)
_stub("position_manager", open_position=lambda *a, **kw: None,
      check_exits=lambda: 0, display_positions=lambda *a: None,
      _fetch_market_price=lambda *a: None)
_stub("price_monitor", record_and_detect=lambda *a: [])
_stub("strategy_arena", run_arena=lambda *a, **kw: None, check_arena_exits=lambda *a: 0)

# Now pre-import scanner so it's in sys.modules
import scanner  # noqa: E402


# ---------------------------------------------------------------------------
# Mock TradeSignal
# ---------------------------------------------------------------------------

@dataclass
class MockSignal:
    market_id: str
    direction: str
    edge: float
    question: str = "Test market?"
    current_price: float = 0.50
    ai_probability: float = 0.60
    raw_edge: float = 0.10
    fee_estimate: float = 0.01
    confidence: float = 0.70
    position_size: float = 50.0
    reliability: str = "medium"
    signals: dict = None

    def __post_init__(self):
        if self.signals is None:
            self.signals = {"news_titles": [], "llm_reasoning": ""}


# ---------------------------------------------------------------------------
# dedup_signals — cooldown
# ---------------------------------------------------------------------------

class TestDedupCooldown:

    def test_dedup_uses_config_cooldown(self, mock_config):
        """Signal seen 1h ago is filtered when cooldown=2h."""
        mock_config.signal_cooldown_hours = 2.0
        mock_config.max_alerts_per_hour = 100  # disable rate limit

        sig = MockSignal(market_id="mkt1", direction="BUY_YES", edge=0.10)
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        with (
            patch.object(scanner, "get_cooldown", return_value=one_hour_ago),
            patch.object(scanner, "set_cooldown"),
            patch.object(scanner, "prune_cooldowns"),
            patch.object(scanner, "insert_signal"),
        ):
            result = scanner.dedup_signals([sig])

        assert len(result) == 0  # filtered — still in cooldown

    def test_dedup_passes_after_cooldown(self, mock_config):
        """Signal seen 3h ago passes when cooldown=2h."""
        mock_config.signal_cooldown_hours = 2.0
        mock_config.max_alerts_per_hour = 100

        sig = MockSignal(market_id="mkt2", direction="BUY_YES", edge=0.10)
        three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

        with (
            patch.object(scanner, "get_cooldown", return_value=three_hours_ago),
            patch.object(scanner, "set_cooldown"),
            patch.object(scanner, "prune_cooldowns"),
            patch.object(scanner, "insert_signal"),
        ):
            result = scanner.dedup_signals([sig])

        assert len(result) == 1

    def test_dedup_rate_limit_uses_config(self, mock_config):
        """Only cfg.max_alerts_per_hour highest-edge signals are returned."""
        mock_config.signal_cooldown_hours = 0.0  # no cooldown filter
        mock_config.max_alerts_per_hour = 3

        signals = [
            MockSignal(market_id=f"mkt{i}", direction="BUY_YES", edge=float(i) * 0.01)
            for i in range(1, 6)  # 5 signals
        ]

        with (
            patch.object(scanner, "get_cooldown", return_value=None),  # no prior alert
            patch.object(scanner, "set_cooldown"),
            patch.object(scanner, "prune_cooldowns"),
            patch.object(scanner, "insert_signal"),
        ):
            result = scanner.dedup_signals(signals)

        assert len(result) == 3
        # Should be the 3 with highest edge
        result_edges = sorted([s.edge for s in result], reverse=True)
        all_edges = sorted([s.edge for s in signals], reverse=True)
        assert result_edges == all_edges[:3]


# ---------------------------------------------------------------------------
# LLM smart gate logic
# ---------------------------------------------------------------------------

class TestLLMGate:
    """Test the smart gate condition in run_scan without calling the full function."""

    def _evaluate_gate(self, signals, news_items, llm_only=False):
        """Replicate the gate logic from run_scan."""
        has_breaking = any(
            item.get("importance", 0) >= 4 or item.get("source") in ("Reuters", "AP", "Bloomberg")
            for item in news_items[:15]
        )
        should_skip = not signals and not has_breaking and not llm_only
        return not should_skip  # True means LLM runs

    def test_llm_gate_skips_when_no_signals_no_breaking(self):
        """No signals, no breaking news, llm_only=False → gate closes (skip LLM)."""
        news = [{"title": "Ordinary news", "importance": 1, "source": "Reddit"}]
        should_run = self._evaluate_gate(signals=[], news_items=news, llm_only=False)
        assert should_run is False

    def test_llm_gate_runs_when_breaking_news(self):
        """Breaking news (importance>=4) → gate opens (run LLM)."""
        news = [{"title": "Breaking!", "importance": 4, "source": "AP"}]
        should_run = self._evaluate_gate(signals=[], news_items=news, llm_only=False)
        assert should_run is True

    def test_llm_gate_runs_when_llm_only(self):
        """llm_only=True always runs LLM regardless of signals."""
        news = [{"title": "Boring news", "importance": 0, "source": "blog"}]
        should_run = self._evaluate_gate(signals=[], news_items=news, llm_only=True)
        assert should_run is True

    def test_llm_gate_runs_when_signals_present(self):
        """Keyword signals present → gate opens."""
        sig = MockSignal(market_id="mkt1", direction="BUY_YES", edge=0.05)
        news = [{"title": "Boring", "importance": 0}]
        should_run = self._evaluate_gate(signals=[sig], news_items=news, llm_only=False)
        assert should_run is True

    def test_llm_gate_runs_for_reuters_source(self):
        """Reuters source triggers has_breaking even with importance=0."""
        news = [{"title": "Reuters story", "importance": 0, "source": "Reuters"}]
        should_run = self._evaluate_gate(signals=[], news_items=news, llm_only=False)
        assert should_run is True

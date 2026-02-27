"""Tests for strategy_arena.py — config-driven active strategies and overrides."""

import sys
import types
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field

# Ensure the real strategy_arena module is loaded (not a stub from another test file)
# Remove any stub that may have been injected by test_exit_manager or test_scanner
sys.modules.pop("strategy_arena", None)

import strategy_arena  # noqa: E402
from strategy_arena import run_arena, STRATEGIES


# ---------------------------------------------------------------------------
# Mock Estimate
# ---------------------------------------------------------------------------

@dataclass
class MockEstimate:
    market_id: str
    question: str
    current_price: float
    ai_probability: float
    confidence: float
    signals: dict = field(default_factory=lambda: {"news_titles": [], "llm_reasoning": ""})
    clob_token_ids: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_estimate(market_id="mkt1", current_price=0.40, ai_prob=0.70, confidence=0.80):
    return MockEstimate(
        market_id=market_id,
        question=f"Will {market_id} happen?",
        current_price=current_price,
        ai_probability=ai_prob,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# run_arena — active strategies
# ---------------------------------------------------------------------------

class TestArenaActiveStrategies:

    def test_arena_uses_config_active_strategies(self, mock_config):
        """Only sniper strategy runs when cfg.active_strategies=['sniper']."""
        mock_config.active_strategies = ["sniper"]
        mock_config.strategy_overrides = {}
        mock_config.max_order_size = 15.0

        estimates = [_make_estimate(current_price=0.30, ai_prob=0.80, confidence=0.70)]
        opened_strategies = []

        def mock_upsert(pos):
            opened_strategies.append(pos["strategy"])

        with (
            patch.object(strategy_arena, "get_positions", return_value=[]),
            patch.object(strategy_arena, "upsert_position", side_effect=mock_upsert),
            patch.object(strategy_arena, "insert_trade"),
            patch.object(strategy_arena, "get_trades", return_value=[]),
        ):
            run_arena(estimates, bankroll=1000.0, live_trading=False)

        assert "sniper" in opened_strategies
        assert "baseline" not in opened_strategies

    def test_arena_multiple_strategies(self, mock_config):
        """Both baseline and conservative run when both are in active_strategies."""
        mock_config.active_strategies = ["baseline", "conservative"]
        mock_config.strategy_overrides = {}
        mock_config.max_order_size = 15.0

        estimates = [_make_estimate(current_price=0.40, ai_prob=0.80, confidence=0.80)]
        opened_strategies = []

        def mock_upsert(pos):
            opened_strategies.append(pos["strategy"])

        with (
            patch.object(strategy_arena, "get_positions", return_value=[]),
            patch.object(strategy_arena, "upsert_position", side_effect=mock_upsert),
            patch.object(strategy_arena, "insert_trade"),
            patch.object(strategy_arena, "get_trades", return_value=[]),
        ):
            run_arena(estimates, bankroll=1000.0, live_trading=False)

        assert "baseline" in opened_strategies
        assert "conservative" in opened_strategies

    def test_arena_skips_inactive_strategies(self, mock_config):
        """Aggressive strategy doesn't open positions when not in active_strategies."""
        mock_config.active_strategies = ["baseline"]
        mock_config.strategy_overrides = {}
        mock_config.max_order_size = 15.0

        estimates = [_make_estimate(current_price=0.40, ai_prob=0.80, confidence=0.80)]
        opened_strategies = []

        def mock_upsert(pos):
            opened_strategies.append(pos["strategy"])

        with (
            patch.object(strategy_arena, "get_positions", return_value=[]),
            patch.object(strategy_arena, "upsert_position", side_effect=mock_upsert),
            patch.object(strategy_arena, "insert_trade"),
            patch.object(strategy_arena, "get_trades", return_value=[]),
        ):
            run_arena(estimates, bankroll=1000.0, live_trading=False)

        assert "aggressive" not in opened_strategies


# ---------------------------------------------------------------------------
# run_arena — strategy overrides
# ---------------------------------------------------------------------------

class TestArenaStrategyOverrides:

    def test_arena_applies_strategy_overrides(self, mock_config):
        """cfg.strategy_overrides overrides StrategyConfig fields before run."""
        mock_config.active_strategies = ["sniper"]
        mock_config.strategy_overrides = {"sniper": {"timeout_hours": 3}}
        mock_config.max_order_size = 15.0

        estimates = [_make_estimate(current_price=0.30, ai_prob=0.80, confidence=0.70)]

        with (
            patch.object(strategy_arena, "get_positions", return_value=[]),
            patch.object(strategy_arena, "upsert_position"),
            patch.object(strategy_arena, "insert_trade"),
            patch.object(strategy_arena, "get_trades", return_value=[]),
        ):
            run_arena(estimates, bankroll=1000.0, live_trading=False)
            assert STRATEGIES["sniper"].timeout_hours == 3


# ---------------------------------------------------------------------------
# run_arena — live scaling respects max_order_size
# ---------------------------------------------------------------------------

class TestArenaLiveScaling:

    def test_arena_live_scaling_uses_config_max_order(self, mock_config):
        """Live order cost is capped at cfg.max_order_size=10, not 20."""
        mock_config.active_strategies = ["baseline"]
        mock_config.strategy_overrides = {}
        mock_config.max_order_size = 10.0

        estimates = [
            MockEstimate(
                market_id="mkt1",
                question="Will X happen?",
                current_price=0.40,
                ai_probability=0.80,
                confidence=0.90,
                signals={"news_titles": [], "llm_reasoning": ""},
                clob_token_ids=["tok_yes", "tok_no"],
            )
        ]

        live_costs = []

        def fake_open_live(market_id, token_id, question, direction, price,
                           size_usd, **kwargs):
            live_costs.append(size_usd)

        mock_live_trader = MagicMock()
        mock_live_trader.get_balance.return_value = 500.0
        mock_live_trader.release_funds_for_signal.return_value = 500.0
        mock_live_trader.open_live_position.side_effect = fake_open_live

        with (
            patch.object(strategy_arena, "get_positions", return_value=[]),
            patch.object(strategy_arena, "upsert_position"),
            patch.object(strategy_arena, "insert_trade"),
            patch.object(strategy_arena, "get_trades", return_value=[]),
        ):
            sys.modules["live_trader"] = mock_live_trader
            try:
                run_arena(estimates, bankroll=1000.0, live_trading=True)
            finally:
                sys.modules.pop("live_trader", None)

        # If any live order was placed, it must not exceed max_order_size
        for cost in live_costs:
            assert cost <= 10.0

"""Tests for edge_calculator.py — config-driven filters and Kelly sizing."""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

import src.config as config_module
from src.config import Config
from src.probability_engine import ProbEstimate
import src.edge_calculator as edge_calculator


def make_config(tmp_path, **overrides):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "private_key": "0x" + "a" * 64,
        "_config_dir": tmp_path,
        "data_dir": str(data_dir),
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def make_estimate(
    ai_probability=0.70,
    current_price=0.50,
    confidence=0.8,
    signals=None,
    market_id="mkt-001",
    question="Will X happen?",
):
    if signals is None:
        signals = {"n_signals": 3, "avg_importance": 4, "source": "Reuters"}
    return ProbEstimate(
        market_id=market_id,
        question=question,
        current_price=current_price,
        ai_probability=ai_probability,
        confidence=confidence,
        signals=signals,
    )


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


def test_calculate_edge_uses_config_min_edge(tmp_path, monkeypatch):
    """Signals with edge below config min_edge_threshold are filtered out."""
    cfg = make_config(tmp_path, min_edge_threshold=0.10, max_kelly_fraction=0.10, min_shares=1)
    monkeypatch.setattr(config_module, "_config", cfg)

    # ai_prob=0.55, price=0.50 → raw edge ~0.05 < 0.10 — should be filtered
    estimate_low = make_estimate(ai_probability=0.55, current_price=0.50)
    result = edge_calculator.calculate_edge(estimate_low, bankroll=10000)
    assert result is None

    # ai_prob=0.70, price=0.50 → raw edge ~0.20 > 0.10 — should pass
    estimate_high = make_estimate(ai_probability=0.70, current_price=0.50)
    result = edge_calculator.calculate_edge(estimate_high, bankroll=10000)
    assert result is not None


def test_calculate_edge_uses_config_max_kelly(tmp_path, monkeypatch):
    """Kelly fraction is capped at config max_kelly_fraction."""
    cfg = make_config(tmp_path, min_edge_threshold=0.02, max_kelly_fraction=0.05, min_shares=1)
    monkeypatch.setattr(config_module, "_config", cfg)

    estimate = make_estimate(ai_probability=0.90, current_price=0.50, confidence=1.0)
    result = edge_calculator.calculate_edge(estimate, bankroll=10000)

    # kelly_fraction in result should be <= max_kelly_fraction (0.05) * confidence
    assert result is not None
    assert result.kelly_fraction <= 0.05


def test_calculate_edge_uses_config_min_shares(tmp_path, monkeypatch):
    """Signals resulting in fewer than min_shares are filtered out."""
    # Set high min_shares so even large edge gets filtered on tiny bankroll
    cfg = make_config(tmp_path, min_edge_threshold=0.02, max_kelly_fraction=0.10, min_shares=10)
    monkeypatch.setattr(config_module, "_config", cfg)

    # Tiny bankroll → very few shares
    estimate = make_estimate(ai_probability=0.60, current_price=0.50, confidence=0.5)
    result = edge_calculator.calculate_edge(estimate, bankroll=1.0)
    assert result is None


def test_find_edges_passes_min_edge(tmp_path, monkeypatch):
    """find_edges with min_edge=None reads from config (default behavior)."""
    cfg = make_config(tmp_path, min_edge_threshold=0.02, max_kelly_fraction=0.10, min_shares=1)
    monkeypatch.setattr(config_module, "_config", cfg)

    estimates = [
        make_estimate(ai_probability=0.70, current_price=0.50, market_id="m1"),
        make_estimate(ai_probability=0.51, current_price=0.50, market_id="m2"),  # tiny edge
    ]
    signals = edge_calculator.find_edges(estimates, bankroll=10000, min_edge=None)
    # At least the clearly-edged one should come through
    signal_ids = [s.market_id for s in signals]
    assert "m1" in signal_ids


def test_expiration_filter(tmp_path, monkeypatch):
    """Markets expiring in less than 1 hour return None."""
    cfg = make_config(tmp_path, min_edge_threshold=0.02, max_kelly_fraction=0.10, min_shares=1)
    monkeypatch.setattr(config_module, "_config", cfg)

    # end_date = 30 minutes from now
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    signals_dict = {"n_signals": 3, "avg_importance": 4, "source": "Reuters", "end_date": soon}
    estimate = make_estimate(ai_probability=0.70, current_price=0.50, signals=signals_dict)

    result = edge_calculator.calculate_edge(estimate, bankroll=10000)
    assert result is None

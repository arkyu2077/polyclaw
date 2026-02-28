"""Tests for order_executor.py — all limits read from config."""

import sys
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(order_result=None):
    """Return a mock ClobClient that succeeds by default."""
    client = MagicMock()
    # get_order_book raises so price-bump path is used (simpler)
    client.get_order_book.side_effect = Exception("no book")
    client.create_order.return_value = MagicMock()
    client.post_order.return_value = order_result or {"orderID": "test-order-123"}
    return client


# ---------------------------------------------------------------------------
# test_order_rejected_when_cost_exceeds_config_max
# ---------------------------------------------------------------------------

def test_order_rejected_when_cost_exceeds_config_max(mock_config, monkeypatch):
    """Order cost > cfg.max_order_size should return None."""
    mock_config.max_order_size = 10.0

    monkeypatch.setattr("src.order_executor._get_client", lambda: _make_mock_client())
    monkeypatch.setattr("src.order_executor.get_daily_pnl", lambda mode: 0.0)
    monkeypatch.setattr("src.order_executor.get_balance", lambda: 1000.0)
    monkeypatch.setattr("src.order_executor.get_positions", lambda mode, status: [])

    from src.order_executor import place_limit_order

    # price=0.60, size=20 → cost=12 > max_order_size=10
    result = place_limit_order(
        token_id="tok1",
        side="BUY",
        price=0.60,
        size=20,
    )
    assert result is None


# ---------------------------------------------------------------------------
# test_order_rejected_when_daily_loss_exceeded
# ---------------------------------------------------------------------------

def test_order_rejected_when_daily_loss_exceeded(mock_config, monkeypatch):
    """Order should be rejected when daily PnL is worse than -cfg.daily_loss_limit."""
    mock_config.daily_loss_limit = 20.0

    monkeypatch.setattr("src.order_executor._get_client", lambda: _make_mock_client())
    monkeypatch.setattr("src.order_executor.get_daily_pnl", lambda mode: -25.0)
    monkeypatch.setattr("src.order_executor.get_balance", lambda: 1000.0)
    monkeypatch.setattr("src.order_executor.get_positions", lambda mode, status: [])

    from src.order_executor import place_limit_order

    # cost=5 < max_order_size default, but daily loss exceeds limit
    result = place_limit_order(
        token_id="tok2",
        side="BUY",
        price=0.50,
        size=10,
    )
    assert result is None


# ---------------------------------------------------------------------------
# test_order_rejected_when_max_positions_reached
# ---------------------------------------------------------------------------

def test_order_rejected_when_max_positions_reached(mock_config, monkeypatch):
    """Order rejected when open positions count >= cfg.max_positions."""
    mock_config.max_positions = 2
    mock_config.max_order_size = 100.0

    # Return 2 open positions (dicts with any content — only len() is checked)
    fake_positions = [{"id": "p1"}, {"id": "p2"}]

    monkeypatch.setattr("src.order_executor._get_client", lambda: _make_mock_client())
    monkeypatch.setattr("src.order_executor.get_daily_pnl", lambda mode: 0.0)
    monkeypatch.setattr("src.order_executor.get_balance", lambda: 1000.0)
    monkeypatch.setattr("src.order_executor.get_positions", lambda mode, status: fake_positions)

    from src.order_executor import place_limit_order

    result = place_limit_order(
        token_id="tok3",
        side="BUY",
        price=0.50,
        size=5,
    )
    assert result is None


# ---------------------------------------------------------------------------
# test_order_accepted_within_limits
# ---------------------------------------------------------------------------

def test_order_accepted_within_limits(mock_config, monkeypatch):
    """Order should succeed when all limits are satisfied."""
    mock_config.max_order_size = 100.0
    mock_config.daily_loss_limit = 50.0
    mock_config.max_positions = 5

    expected_result = {"orderID": "abc-456", "status": "matched"}
    mock_client = _make_mock_client(order_result=expected_result)

    monkeypatch.setattr("src.order_executor._get_client", lambda: mock_client)
    monkeypatch.setattr("src.order_executor.get_daily_pnl", lambda mode: 0.0)
    monkeypatch.setattr("src.order_executor.get_balance", lambda: 1000.0)
    monkeypatch.setattr("src.order_executor.get_positions", lambda mode, status: [])

    from src.order_executor import place_limit_order

    # price=0.50, size=10 → cost=5, well within limits
    result = place_limit_order(
        token_id="tok4",
        side="BUY",
        price=0.50,
        size=10,
    )
    assert result is not None
    assert isinstance(result, dict)
    assert result.get("orderID") == "abc-456"

"""Tests for exit_manager.py — config-driven timeout/stale/drift logic."""

import sys
import types
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import src.exit_manager as exit_manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(market_id="mkt1", token_id="tok1", entry_hours_ago=4,
                   target_price=0.80, stop_loss=0.30):
    entry_time = (datetime.now(timezone.utc) - timedelta(hours=entry_hours_ago)).isoformat()
    return {
        "market_id": market_id,
        "token_id": token_id,
        "entry_time": entry_time,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "status": "open",
    }


def _make_order(order_id="ord1", age_hours=0, price=0.50, outcome="YES",
                market_id="mkt1", original_size=10.0, size_matched=0.0):
    created_ts = int((datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp())
    return {
        "id": order_id,
        "created_at": created_ts,
        "price": price,
        "market": market_id,
        "outcome": outcome,
        "original_size": original_size,
        "size_matched": size_matched,
    }


# ---------------------------------------------------------------------------
# check_live_exits — timeout
# ---------------------------------------------------------------------------

class TestLiveExitTimeout:

    def test_live_exit_timeout_uses_config(self, mock_config):
        """Position opened 4h ago should be closed when cfg.live_timeout_hours=3."""
        mock_config.live_timeout_hours = 3.0

        pos = _make_position(entry_hours_ago=4)
        mock_market = {"tokens": [{"token_id": "tok1", "price": "0.50"}]}

        mock_client = MagicMock()
        mock_client.get_market.return_value = mock_market

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "get_positions", return_value=[pos]),
            patch.object(exit_manager, "check_pending_orders"),
            patch.object(exit_manager, "close_live_position", return_value=True) as mock_close,
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.check_live_exits()

        assert result == 1
        mock_close.assert_called_once_with(pos, "TIMEOUT")

    def test_live_exit_no_timeout_within_config(self, mock_config):
        """Position opened 4h ago should NOT be closed when cfg.live_timeout_hours=10."""
        mock_config.live_timeout_hours = 10.0

        pos = _make_position(entry_hours_ago=4)
        mock_market = {"tokens": [{"token_id": "tok1", "price": "0.50"}]}

        mock_client = MagicMock()
        mock_client.get_market.return_value = mock_market

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "get_positions", return_value=[pos]),
            patch.object(exit_manager, "check_pending_orders"),
            patch.object(exit_manager, "close_live_position", return_value=True) as mock_close,
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.check_live_exits()

        assert result == 0
        mock_close.assert_not_called()


# ---------------------------------------------------------------------------
# cleanup_stale_orders — stale hours
# ---------------------------------------------------------------------------

class TestStaleOrderTimeout:

    def test_stale_order_uses_config_hours(self, mock_config):
        """Order 8h old should be cancelled when cfg.stale_order_hours=6."""
        mock_config.stale_order_hours = 6.0
        mock_config.price_drift_threshold = 0.99  # disable drift check

        order = _make_order(age_hours=8, original_size=10.0, size_matched=0.0)

        mock_client = MagicMock()
        mock_client.get_orders.return_value = [order]
        mock_client.get_market.return_value = {"tokens": [], "end_date_iso": ""}
        mock_client.cancel.return_value = None

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.cleanup_stale_orders()

        assert result == 1
        mock_client.cancel.assert_called_once_with("ord1")

    def test_stale_order_not_cancelled_within_hours(self, mock_config):
        """Order 3h old should NOT be cancelled when cfg.stale_order_hours=6."""
        mock_config.stale_order_hours = 6.0
        mock_config.price_drift_threshold = 0.99  # disable drift check

        order = _make_order(age_hours=3, original_size=10.0, size_matched=0.0)

        mock_client = MagicMock()
        mock_client.get_orders.return_value = [order]
        mock_client.get_market.return_value = {"tokens": [], "end_date_iso": ""}

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.cleanup_stale_orders()

        assert result == 0
        mock_client.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# cleanup_stale_orders — price drift
# ---------------------------------------------------------------------------

class TestPriceDrift:

    def test_price_drift_uses_config_threshold(self, mock_config):
        """Order with 15% drift should be cancelled when cfg.price_drift_threshold=0.10."""
        mock_config.stale_order_hours = 9999.0  # disable timeout
        mock_config.price_drift_threshold = 0.10

        order = _make_order(price=0.50, outcome="YES", original_size=10.0, size_matched=0.0)
        # Current price 0.575 → drift = |0.575-0.50|/0.50 = 15%
        mock_market = {
            "tokens": [{"outcome": "YES", "price": "0.575"}],
            "end_date_iso": "",
        }

        mock_client = MagicMock()
        mock_client.get_orders.return_value = [order]
        mock_client.get_market.return_value = mock_market
        mock_client.cancel.return_value = None

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.cleanup_stale_orders()

        assert result == 1
        mock_client.cancel.assert_called_once()

    def test_price_drift_within_threshold(self, mock_config):
        """Order with 15% drift should NOT be cancelled when cfg.price_drift_threshold=0.30."""
        mock_config.stale_order_hours = 9999.0  # disable timeout
        mock_config.price_drift_threshold = 0.30

        order = _make_order(price=0.50, outcome="YES", original_size=10.0, size_matched=0.0)
        # Current price 0.575 → drift = 15%
        mock_market = {
            "tokens": [{"outcome": "YES", "price": "0.575"}],
            "end_date_iso": "",
        }

        mock_client = MagicMock()
        mock_client.get_orders.return_value = [order]
        mock_client.get_market.return_value = mock_market

        with (
            patch.object(exit_manager, "_get_client", return_value=mock_client),
            patch.object(exit_manager, "add_notification"),
        ):
            result = exit_manager.cleanup_stale_orders()

        assert result == 0
        mock_client.cancel.assert_not_called()

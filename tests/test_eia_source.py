"""Tests for eia_source.py — EIA energy data."""
import json
import pytest
from unittest.mock import patch, MagicMock

from src.eia_source import fetch_eia, _change_importance


MOCK_EIA_RESPONSE = {
    "response": {
        "data": [
            {"value": 78.50, "period": "2025-03-01"},
            {"value": 76.00, "period": "2025-02-28"},
        ]
    }
}


class TestChangeImportance:
    def test_small_change(self):
        assert _change_importance(0.5) == 2

    def test_moderate_change(self):
        assert _change_importance(2.0) == 3

    def test_large_change(self):
        assert _change_importance(4.0) == 4

    def test_extreme_change(self):
        assert _change_importance(7.0) == 5

    def test_negative_change(self):
        assert _change_importance(-4.0) == 4


class TestFetchEia:
    def test_returns_empty_without_key(self, mock_config):
        """No API key → silent empty return."""
        mock_config.eia_api_key = ""
        assert fetch_eia() == []

    def test_parses_price_data(self, mock_config):
        mock_config.eia_api_key = "test-key"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_EIA_RESPONSE

        with patch("src.eia_source.httpx.get", return_value=mock_resp):
            items = fetch_eia()

        assert len(items) > 0
        item = items[0]
        assert item["source"] == "eia"
        assert item["id"].startswith("eia-")
        assert "$" in item["title"]
        assert "78.50" in item["title"]

    def test_calculates_change_on_second_fetch(self, mock_config):
        """Second fetch should show % change from first fetch."""
        mock_config.eia_api_key = "test-key"

        # First fetch: price = 78.50
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = MOCK_EIA_RESPONSE

        # Second fetch (manually reset state): price = 80.00
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            "response": {
                "data": [
                    {"value": 80.00, "period": "2025-03-02"},
                ]
            }
        }

        with patch("src.eia_source.httpx.get", return_value=resp1):
            first = fetch_eia()

        # Manually reset last_fetch to bypass interval
        import src.eia_source as eia_mod
        state = eia_mod._load_state()
        state["last_fetch"] = 0
        eia_mod._save_state(state)

        with patch("src.eia_source.httpx.get", return_value=resp2):
            second = fetch_eia()

        assert len(second) > 0
        # Should contain % change
        assert "%" in second[0]["title"]

    def test_respects_min_interval(self, mock_config):
        mock_config.eia_api_key = "test-key"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_EIA_RESPONSE

        with patch("src.eia_source.httpx.get", return_value=mock_resp):
            first = fetch_eia()
            second = fetch_eia()

        assert len(first) > 0
        assert second == []

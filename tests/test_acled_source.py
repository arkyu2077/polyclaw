"""Tests for acled_source.py — ACLED armed conflict data (OAuth auth)."""
import json
import pytest
from unittest.mock import patch, MagicMock

from src.acled_source import fetch_acled, _fatalities_importance


MOCK_ACLED_RESPONSE = {
    "data": [
        {
            "event_id_cnty": "EVT001",
            "event_date": "2025-03-01",
            "event_type": "Battles",
            "sub_event_type": "Armed clash",
            "country": "Syria",
            "location": "Aleppo",
            "fatalities": "15",
            "source": "ACLED",
        },
        {
            "event_id_cnty": "EVT002",
            "event_date": "2025-03-01",
            "event_type": "Explosions/Remote violence",
            "sub_event_type": "Shelling",
            "country": "Ukraine",
            "location": "Donetsk",
            "fatalities": "0",
            "source": "ACLED",
        },
    ]
}

MOCK_TOKEN_RESPONSE = {
    "access_token": "test-token-123",
    "expires_in": 86400,
    "token_type": "Bearer",
}


class TestFatalitiesImportance:
    def test_zero(self):
        assert _fatalities_importance(0) == 2

    def test_low(self):
        assert _fatalities_importance(5) == 3

    def test_medium(self):
        assert _fatalities_importance(25) == 4

    def test_high(self):
        assert _fatalities_importance(100) == 5


class TestFetchAcled:
    def test_returns_empty_without_credentials(self, mock_config):
        """No credentials → silent empty return."""
        mock_config.acled_email = ""
        mock_config.acled_password = ""
        assert fetch_acled() == []

    def test_returns_empty_without_password(self, mock_config):
        mock_config.acled_email = "test@example.com"
        mock_config.acled_password = ""
        assert fetch_acled() == []

    def test_parses_events_with_credentials(self, mock_config):
        mock_config.acled_email = "test@example.com"
        mock_config.acled_password = "testpass"

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = MOCK_TOKEN_RESPONSE

        data_resp = MagicMock()
        data_resp.status_code = 200
        data_resp.json.return_value = MOCK_ACLED_RESPONSE

        def route(url_or_method, *args, **kwargs):
            # httpx.post for token, httpx.get for data
            if "oauth/token" in str(url_or_method):
                return token_resp
            return data_resp

        with patch("src.acled_source.httpx.post", return_value=token_resp), \
             patch("src.acled_source.httpx.get", return_value=data_resp):
            items = fetch_acled()

        assert len(items) == 2
        # First item should be highest fatalities (sorted)
        assert "15 fatalities" in items[0]["title"]
        assert items[0]["source"] == "acled"
        assert items[0]["id"].startswith("acled-")

    def test_importance_based_on_fatalities(self, mock_config):
        mock_config.acled_email = "test@example.com"
        mock_config.acled_password = "testpass"

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = MOCK_TOKEN_RESPONSE

        data_resp = MagicMock()
        data_resp.status_code = 200
        data_resp.json.return_value = MOCK_ACLED_RESPONSE

        with patch("src.acled_source.httpx.post", return_value=token_resp), \
             patch("src.acled_source.httpx.get", return_value=data_resp):
            items = fetch_acled()

        # 15 fatalities → importance 4
        assert items[0]["importance"] == 4
        # 0 fatalities → importance 2
        assert items[1]["importance"] == 2

    def test_respects_min_interval(self, mock_config):
        mock_config.acled_email = "test@example.com"
        mock_config.acled_password = "testpass"

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = MOCK_TOKEN_RESPONSE

        data_resp = MagicMock()
        data_resp.status_code = 200
        data_resp.json.return_value = MOCK_ACLED_RESPONSE

        with patch("src.acled_source.httpx.post", return_value=token_resp), \
             patch("src.acled_source.httpx.get", return_value=data_resp):
            first = fetch_acled()
            second = fetch_acled()

        assert len(first) > 0
        assert second == []

    def test_handles_token_failure(self, mock_config):
        mock_config.acled_email = "test@example.com"
        mock_config.acled_password = "badpass"

        token_resp = MagicMock()
        token_resp.status_code = 401
        token_resp.json.return_value = {"error": "invalid_grant"}

        with patch("src.acled_source.httpx.post", return_value=token_resp):
            items = fetch_acled()

        assert items == []

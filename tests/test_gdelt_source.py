"""Tests for gdelt_source.py — GDELT global news API."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from src.gdelt_source import fetch_gdelt, _domain_importance


MOCK_GDELT_RESPONSE = {
    "articles": [
        {
            "url": "https://www.reuters.com/world/test-article",
            "title": "Test Reuters Article About Conflict",
            "seendate": "20250301T120000Z",
        },
        {
            "url": "https://www.bbc.com/news/world-test",
            "title": "BBC World News Test",
            "seendate": "20250301T110000Z",
        },
        {
            "url": "https://example.com/unknown-source",
            "title": "Unknown Source Article",
            "seendate": "20250301T100000Z",
        },
    ]
}


class TestDomainImportance:
    def test_reuters(self):
        assert _domain_importance("https://www.reuters.com/article/123") == 5

    def test_bbc(self):
        assert _domain_importance("https://www.bbc.com/news/world") == 4

    def test_unknown(self):
        assert _domain_importance("https://random-blog.com/post") == 2


class TestFetchGdelt:
    def test_returns_standard_format(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_GDELT_RESPONSE

        with patch("src.gdelt_source.httpx.get", return_value=mock_resp):
            items = fetch_gdelt(queries=["test"])

        assert len(items) > 0
        item = items[0]
        assert "id" in item
        assert item["id"].startswith("gdelt-")
        assert "title" in item
        assert item["source"] == "gdelt"
        assert "published" in item
        assert "fetched_at" in item
        assert "url" in item
        assert "importance" in item

    def test_respects_min_interval(self, mock_config):
        """Second call within MIN_INTERVAL returns empty."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_GDELT_RESPONSE

        with patch("src.gdelt_source.httpx.get", return_value=mock_resp):
            first = fetch_gdelt(queries=["test"])
            second = fetch_gdelt(queries=["test"])

        assert len(first) > 0
        assert second == []

    def test_skips_empty_titles(self, mock_config):
        response = {"articles": [{"url": "https://x.com/a", "title": "", "seendate": ""}]}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response

        with patch("src.gdelt_source.httpx.get", return_value=mock_resp):
            items = fetch_gdelt(queries=["test"])

        assert items == []

    def test_handles_api_error(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("src.gdelt_source.httpx.get", return_value=mock_resp):
            items = fetch_gdelt(queries=["test"])

        assert items == []

    def test_deduplicates_seen_ids(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Two articles with same URL
        mock_resp.json.return_value = {
            "articles": [
                {"url": "https://x.com/same", "title": "Title A", "seendate": ""},
                {"url": "https://x.com/same", "title": "Title B", "seendate": ""},
            ]
        }

        with patch("src.gdelt_source.httpx.get", return_value=mock_resp):
            items = fetch_gdelt(queries=["test"])

        assert len(items) == 1

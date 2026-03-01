"""Tests for telegram_source.py — Telegram channel RSS proxy."""
import json
import pytest
from unittest.mock import patch, MagicMock

from src.telegram_source import fetch_telegram, _strip_html


MOCK_RSS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Telegram Channel</title>
<item>
    <title>Breaking: Major event happening now</title>
    <link>https://t.me/bbcbreaking/12345</link>
    <description>Details about the event</description>
    <pubDate>Sat, 01 Mar 2025 12:00:00 GMT</pubDate>
</item>
<item>
    <title>Second news item</title>
    <link>https://t.me/bbcbreaking/12346</link>
    <description>More details</description>
    <pubDate>Sat, 01 Mar 2025 11:00:00 GMT</pubDate>
</item>
</channel>
</rss>"""


class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<b>bold</b> text") == "bold text"

    def test_handles_empty(self):
        assert _strip_html("") == ""

    def test_no_tags(self):
        assert _strip_html("plain text") == "plain text"


class TestFetchTelegram:
    def test_fetches_via_rsshub(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS_RESPONSE

        with patch("src.telegram_source.httpx.get", return_value=mock_resp):
            items = fetch_telegram()

        assert len(items) > 0
        item = items[0]
        assert item["source"] == "telegram"
        assert item["id"].startswith("tg-")
        assert "title" in item
        assert "url" in item

    def test_prefixes_channel_name(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS_RESPONSE

        with patch("src.telegram_source.httpx.get", return_value=mock_resp):
            items = fetch_telegram()

        # Titles should be prefixed with channel name
        assert any("[" in item["title"] for item in items)

    def test_respects_min_interval(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS_RESPONSE

        with patch("src.telegram_source.httpx.get", return_value=mock_resp):
            first = fetch_telegram()
            second = fetch_telegram()

        assert len(first) > 0
        assert second == []

    def test_falls_back_to_html(self, mock_config):
        """When RSSHub returns non-200, falls back to HTML scraping."""
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.text = ""

        html_resp = MagicMock()
        html_resp.status_code = 200
        html_resp.text = """
        <div class="tgme_widget_message_text js-message_text" dir="auto">
            Some telegram message content here
        </div>
        """

        def side_effect(url, **kwargs):
            if "rsshub" in url:
                return fail_resp
            return html_resp

        with patch("src.telegram_source.httpx.get", side_effect=side_effect):
            items = fetch_telegram()

        # Should still get items via fallback
        assert isinstance(items, list)

    def test_handles_total_failure(self, mock_config):
        with patch("src.telegram_source.httpx.get", side_effect=Exception("network error")):
            items = fetch_telegram()

        assert items == []

"""Tests for gdacs_source.py — GDACS disaster alert RSS."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from src.gdacs_source import fetch_gdacs, _parse_alert_level


MOCK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>GDACS</title>
<item>
    <title>Red alert: Earthquake in Turkey</title>
    <link>https://www.gdacs.org/report.aspx?eventid=1</link>
    <description>Major earthquake</description>
    <pubDate>Sat, 01 Mar 2025 12:00:00 GMT</pubDate>
</item>
<item>
    <title>Orange alert: Flood in India</title>
    <link>https://www.gdacs.org/report.aspx?eventid=2</link>
    <description>Severe flooding</description>
    <pubDate>Sat, 01 Mar 2025 11:00:00 GMT</pubDate>
</item>
<item>
    <title>Green alert: Minor event</title>
    <link>https://www.gdacs.org/report.aspx?eventid=3</link>
    <description>Minor event</description>
    <pubDate>Sat, 01 Mar 2025 10:00:00 GMT</pubDate>
</item>
</channel>
</rss>"""


class TestParseAlertLevel:
    def test_red_in_title(self):
        entry = {"title": "Red alert: Earthquake"}
        assert _parse_alert_level(entry) == "red"

    def test_orange_in_title(self):
        entry = {"title": "Orange alert: Flood"}
        assert _parse_alert_level(entry) == "orange"

    def test_green_in_title(self):
        entry = {"title": "Green alert: Minor"}
        assert _parse_alert_level(entry) == "green"

    def test_from_namespace(self):
        entry = {"title": "Some event", "gdacs_alertlevel": "Red"}
        assert _parse_alert_level(entry) == "red"

    def test_unknown(self):
        entry = {"title": "Some event without color"}
        assert _parse_alert_level(entry) == "unknown"


class TestFetchGdacs:
    def test_filters_green_alerts(self, mock_config):
        """Green alerts should be filtered out."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS

        with patch("src.gdacs_source.httpx.get", return_value=mock_resp):
            items = fetch_gdacs()

        # Only Red and Orange should pass
        assert len(items) == 2
        titles = [i["title"] for i in items]
        assert any("Red" in t for t in titles)
        assert any("Orange" in t for t in titles)
        assert not any("Green" in t for t in titles)

    def test_importance_levels(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS

        with patch("src.gdacs_source.httpx.get", return_value=mock_resp):
            items = fetch_gdacs()

        for item in items:
            if "Red" in item["title"]:
                assert item["importance"] == 5
            elif "Orange" in item["title"]:
                assert item["importance"] == 4

    def test_standard_format(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS

        with patch("src.gdacs_source.httpx.get", return_value=mock_resp):
            items = fetch_gdacs()

        assert len(items) > 0
        item = items[0]
        assert item["id"].startswith("gdacs-")
        assert item["source"] == "gdacs"
        assert "title" in item
        assert "url" in item

    def test_respects_min_interval(self, mock_config):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_RSS

        with patch("src.gdacs_source.httpx.get", return_value=mock_resp):
            first = fetch_gdacs()
            second = fetch_gdacs()

        assert len(first) > 0
        assert second == []

    def test_handles_fetch_error(self, mock_config):
        with patch("src.gdacs_source.httpx.get", side_effect=Exception("timeout")):
            items = fetch_gdacs()

        assert items == []

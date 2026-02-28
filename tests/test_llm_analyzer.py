"""Tests for llm_analyzer.py — prompt building, provider routing, API key guard."""

import json
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_news(n=5):
    return [{"title": f"News item {i}", "source": "Reuters"} for i in range(n)]


def _make_markets(n=5, ids=None):
    markets = []
    for i in range(n):
        mid = (ids[i] if ids and i < len(ids) else f"market_{i}")
        markets.append({
            "id": mid,
            "question": f"Question {i}?",
            "volume": 1000 * i,
            "outcomePrices": ["0.50", "0.50"],
        })
    return markets


def _valid_api_response(signals=None):
    """Return a mock httpx response with valid JSON containing signals."""
    if signals is None:
        signals = [
            {
                "news_index": 0,
                "market_index": 0,
                "direction": "YES_UP",
                "estimated_probability": 0.70,
                "confidence": 0.80,
                "reasoning": "test reasoning",
            }
        ]
    return json.dumps({"signals": signals})


# ---------------------------------------------------------------------------
# build_prompt — caps at 12 markets
# ---------------------------------------------------------------------------

class TestBuildPrompt:

    def test_build_prompt_caps_at_12_markets(self):
        """Only 12 markets appear in the prompt even when 20 are passed."""
        from src.llm_analyzer import build_prompt
        news = _make_news(3)
        markets = _make_markets(20)
        prompt = build_prompt(news, markets)
        # Count market entries by index prefix "[0]" ... "[11]" but not "[12]"
        assert "[11]" in prompt
        assert "[12]" not in prompt

    def test_build_prompt_prioritizes_matched_ids(self):
        """Matched market IDs appear first in the prompt."""
        from src.llm_analyzer import build_prompt
        # 10 markets, last 3 are "priority"
        all_ids = [f"mkt_{i}" for i in range(10)]
        priority_ids = {"mkt_7", "mkt_8", "mkt_9"}
        markets = _make_markets(10, ids=all_ids)
        # Give priority markets distinctive questions
        for m in markets:
            if m["id"] in priority_ids:
                m["question"] = f"PRIORITY: {m['id']}?"

        news = _make_news(3)
        prompt = build_prompt(news, markets, matched_market_ids=priority_ids)

        # Priority markets should appear before index [3]
        # Find positions of PRIORITY markers
        first_priority_pos = min(
            prompt.find(f"PRIORITY: mkt_{i}?") for i in [7, 8, 9]
            if f"PRIORITY: mkt_{i}?" in prompt
        )
        # And a non-priority market at some later position
        non_priority = "Question 0?"  # mkt_0 is not in priority set
        non_priority_pos = prompt.find(non_priority)
        assert first_priority_pos < non_priority_pos


# ---------------------------------------------------------------------------
# analyze_news_batch — provider routing
# ---------------------------------------------------------------------------

class TestAnalyzeRouting:

    def test_analyze_routes_to_custom_base_url(self, mock_config):
        """cfg.llm_base_url routes openai/gemini/anthropic to custom endpoint."""
        mock_config.llm_provider = "openai"
        mock_config.llm_api_key = "sk-test"
        mock_config.llm_model = "my-model"
        mock_config.llm_base_url = "http://127.0.0.1:8045/v1"

        resp_text = _valid_api_response()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": resp_text}}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            from src.llm_analyzer import analyze_news_batch
            analyze_news_batch(_make_news(2), _make_markets(2))

        called_url = mock_post.call_args[0][0]
        assert "127.0.0.1:8045" in called_url
        assert called_url.endswith("/chat/completions")

    def test_analyze_routes_to_gemini(self, mock_config):
        """cfg.llm_provider='gemini' calls Gemini URL."""
        mock_config.llm_provider = "gemini"
        mock_config.llm_api_key = "test-gemini-key"
        mock_config.llm_model = ""

        resp_text = _valid_api_response()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": resp_text}]}}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            from src.llm_analyzer import analyze_news_batch
            analyze_news_batch(_make_news(2), _make_markets(2))

        called_url = mock_post.call_args[0][0]
        assert "generativelanguage.googleapis.com" in called_url

    def test_analyze_routes_to_openai(self, mock_config):
        """cfg.llm_provider='openai' calls OpenAI URL."""
        mock_config.llm_provider = "openai"
        mock_config.llm_api_key = "test-openai-key"
        mock_config.llm_model = ""

        resp_text = _valid_api_response()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": resp_text}}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            from src.llm_analyzer import analyze_news_batch
            analyze_news_batch(_make_news(2), _make_markets(2))

        called_url = mock_post.call_args[0][0]
        assert "api.openai.com" in called_url

    def test_analyze_routes_to_anthropic(self, mock_config):
        """cfg.llm_provider='anthropic' calls Anthropic URL."""
        mock_config.llm_provider = "anthropic"
        mock_config.llm_api_key = "test-anthropic-key"
        mock_config.llm_model = ""

        resp_text = _valid_api_response()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": resp_text}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            from src.llm_analyzer import analyze_news_batch
            analyze_news_batch(_make_news(2), _make_markets(2))

        called_url = mock_post.call_args[0][0]
        assert "api.anthropic.com" in called_url

    def test_analyze_skips_without_api_key(self, mock_config):
        """Returns empty list when llm_api_key is empty string."""
        mock_config.llm_provider = "gemini"
        mock_config.llm_api_key = ""
        mock_config.llm_model = ""

        with patch("httpx.post") as mock_post:
            from src.llm_analyzer import analyze_news_batch
            result = analyze_news_batch(_make_news(2), _make_markets(2))

        assert result == []
        mock_post.assert_not_called()

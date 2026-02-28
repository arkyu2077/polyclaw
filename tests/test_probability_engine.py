"""Tests for probability_engine.py — discount logic and merge behavior."""

import pytest
import src.config as config_module
from src.config import Config
from src.probability_engine import ProbEstimate, discount_ai_probability, merge_llm_estimates


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


def make_estimate(ai_probability=0.70, current_price=0.50, market_id="mkt-001", confidence=0.8):
    return ProbEstimate(
        market_id=market_id,
        question="Will X happen?",
        current_price=current_price,
        ai_probability=ai_probability,
        confidence=confidence,
        signals={"n_signals": 1, "avg_importance": 3, "source": "Reuters"},
    )


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


def test_discount_uses_config(tmp_path, monkeypatch):
    """discount_ai_probability uses ai_estimate_discount from config."""
    cfg = make_config(tmp_path, ai_estimate_discount=0.3)
    monkeypatch.setattr(config_module, "_config", cfg)

    # ai=0.80, market=0.50 → discounted = 0.50 + (0.80 - 0.50) * 0.3 = 0.59
    result = discount_ai_probability(0.80, 0.50)
    assert abs(result - 0.59) < 1e-4


def test_discount_default(tmp_path, monkeypatch):
    """discount_ai_probability uses default 0.5 discount correctly."""
    cfg = make_config(tmp_path, ai_estimate_discount=0.5)
    monkeypatch.setattr(config_module, "_config", cfg)

    # ai=0.80, market=0.50 → discounted = 0.50 + (0.80 - 0.50) * 0.5 = 0.65
    result = discount_ai_probability(0.80, 0.50)
    assert abs(result - 0.65) < 1e-4


def test_merge_llm_estimates_applies_discount(tmp_path, monkeypatch):
    """merge_llm_estimates applies discounted AI probability, not raw."""
    cfg = make_config(tmp_path, ai_estimate_discount=0.5)
    monkeypatch.setattr(config_module, "_config", cfg)

    keyword_estimates = [make_estimate(ai_probability=0.60, current_price=0.50, market_id="m1")]

    llm_signals = [
        {
            "market_id": "m1",
            "question": "Will X happen?",
            "current_yes": 0.50,
            "estimated_probability": 0.90,  # raw AI — should be discounted
            "confidence": 0.85,
            "news_title": "Breaking news",
            "reasoning": "Strong signal",
        }
    ]

    merged = merge_llm_estimates(keyword_estimates, llm_signals)

    assert len(merged) == 1
    est = merged[0]
    # With discount=0.5: 0.50 + (0.90 - 0.50) * 0.5 = 0.70
    expected = 0.50 + (0.90 - 0.50) * 0.5
    assert abs(est.ai_probability - expected) < 1e-4
    # Raw probability is stored for reference
    assert est.signals.get("raw_ai_probability") == 0.90

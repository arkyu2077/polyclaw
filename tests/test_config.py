"""Tests for config.py — default values, YAML loading, and env var handling."""

import os
import pytest
from pathlib import Path
import config as config_module
from config import Config, load_config


@pytest.fixture(autouse=True)
def reset_config_state(monkeypatch):
    """Reset global config before each test."""
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


def make_config(tmp_path, **overrides):
    """Helper to make a Config with required fields."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "private_key": "0x" + "a" * 64,
        "_config_dir": tmp_path,
        "data_dir": str(data_dir),
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def test_default_values(tmp_path):
    """All key fields have correct defaults."""
    cfg = make_config(tmp_path)

    # Position sizing
    assert cfg.max_position_pct == 0.15
    assert cfg.cooldown_hours == 1.0
    assert cfg.timeout_hours == 24.0
    assert cfg.timeout_move_threshold == 0.02
    assert cfg.kelly_fraction == 0.5
    assert cfg.fee_rate == 0.003

    # Exit strategy
    assert cfg.tp_ratio == 0.70
    assert cfg.high_conf_tp_ratio == 0.85
    assert cfg.low_conf_tp_ratio == 0.55
    assert cfg.sl_ratio == 0.75
    assert cfg.wide_sl_ratio == 0.65
    assert cfg.tight_sl_ratio == 0.82
    assert cfg.trailing_stop_activation == 0.5
    assert cfg.trailing_stop_distance == 0.30

    # Live exit
    assert cfg.live_timeout_hours == 6.0
    assert cfg.stale_order_hours == 12.0
    assert cfg.price_drift_threshold == 0.20

    # Edge calculator
    assert cfg.min_edge_threshold == 0.02
    assert cfg.max_kelly_fraction == 0.10
    assert cfg.min_shares == 5

    # Signal dedup
    assert cfg.signal_cooldown_hours == 4.0
    assert cfg.max_alerts_per_hour == 5

    # LLM
    assert cfg.ai_estimate_discount == 0.5
    assert cfg.llm_provider == "file"
    assert cfg.llm_api_key == ""
    assert cfg.llm_model == ""

    # Arena
    assert cfg.active_strategies == ["baseline"]
    assert cfg.strategy_overrides == {}

    # Safety limits
    assert cfg.max_order_size == 15.0
    assert cfg.daily_loss_limit == 30.0
    assert cfg.max_positions == 4
    assert cfg.min_edge == 0.02


def test_yaml_override_new_fields(tmp_path, monkeypatch):
    """YAML values override defaults for allowed fields."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "tp_ratio: 0.80\n"
        "active_strategies:\n"
        "  - baseline\n"
        "  - sniper\n"
        "min_edge_threshold: 0.05\n"
        "max_kelly_fraction: 0.08\n"
    )
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    cfg = load_config(config_path=config_yaml)

    assert cfg.tp_ratio == 0.80
    assert cfg.active_strategies == ["baseline", "sniper"]
    assert cfg.min_edge_threshold == 0.05
    assert cfg.max_kelly_fraction == 0.08


def test_yaml_allowed_whitelist(tmp_path, monkeypatch):
    """Keys NOT in YAML_ALLOWED are silently ignored."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "tp_ratio: 0.80\n"
        "private_key: 0x" + "c" * 64 + "\n"  # NOT whitelisted — should be ignored
        "wallet_address: 0xdeadbeef\n"         # NOT whitelisted
        "discord_webhook: https://evil.com\n"  # NOT whitelisted
    )
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    cfg = load_config(config_path=config_yaml)

    # tp_ratio is allowed and should be overridden
    assert cfg.tp_ratio == 0.80
    # private_key must come from env, not yaml
    assert cfg.private_key == "0x" + "b" * 64
    # wallet_address not in yaml_allowed — defaults to ""
    assert cfg.wallet_address == ""


def test_active_strategies_is_list(tmp_path, monkeypatch):
    """active_strategies loads as a Python list from YAML."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "active_strategies:\n"
        "  - baseline\n"
        "  - sniper\n"
        "  - conservative\n"
    )
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    cfg = load_config(config_path=config_yaml)

    assert isinstance(cfg.active_strategies, list)
    assert cfg.active_strategies == ["baseline", "sniper", "conservative"]


def test_strategy_overrides_is_dict(tmp_path, monkeypatch):
    """strategy_overrides loads as a Python dict from YAML."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "strategy_overrides:\n"
        "  sniper:\n"
        "    min_edge: 0.05\n"
        "  baseline:\n"
        "    min_edge: 0.02\n"
    )
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    cfg = load_config(config_path=config_yaml)

    assert isinstance(cfg.strategy_overrides, dict)
    assert "sniper" in cfg.strategy_overrides
    assert cfg.strategy_overrides["sniper"]["min_edge"] == 0.05


def test_llm_api_key_from_env(tmp_path, monkeypatch):
    """llm_api_key is read from LLM_API_KEY env var, not YAML."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("tp_ratio: 0.70\n")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-key")

    cfg = load_config(config_path=config_yaml)

    assert cfg.llm_api_key == "sk-test-secret-key"


def test_llm_provider_from_yaml(tmp_path, monkeypatch):
    """llm_provider in YAML overrides the default 'file'."""
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("llm_provider: gemini\n")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    cfg = load_config(config_path=config_yaml)

    assert cfg.llm_provider == "gemini"

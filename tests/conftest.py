"""Shared fixtures for all test modules."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub external packages that aren't installed in test environment
# These must be set BEFORE any src module tries to import them.
# ---------------------------------------------------------------------------

def _ensure_stub(name, **attrs):
    """Create a stub module only if it can't be imported."""
    if name in sys.modules:
        return
    try:
        __import__(name)
    except Exception:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        # Make sub-attribute access return MagicMock by default
        if not attrs:
            mod = MagicMock()
            mod.__name__ = name
        sys.modules[name] = mod


# py_clob_client — required by order_executor.py, position_tracker.py
_ensure_stub("py_clob_client")
_ensure_stub("py_clob_client.client")
_ensure_stub("py_clob_client.clob_types")

# news_ingestion uses relative import `from .config` — stub if not importable
_ensure_stub("news_ingestion", ingest=lambda: [])

# Modules that import from news_ingestion or have complex deps
_ensure_stub("market_cache", get_markets=lambda: [])
_ensure_stub("event_parser", parse_all=lambda *a, **kw: [], parse_with_llm=lambda *a, **kw: [])
_ensure_stub("price_monitor", record_and_detect=lambda *a: [])
_ensure_stub("twitter_source")
_ensure_stub("economic_calendar")
_ensure_stub("volume_monitor")
_ensure_stub("reddit_source")
_ensure_stub("weather_source")
_ensure_stub("sports_odds")

import config as config_module
from config import Config


@pytest.fixture
def reset_config():
    """Reset global config after each test."""
    yield
    config_module._config = None


@pytest.fixture
def mock_config(tmp_path, monkeypatch):
    """Create a Config with safe defaults and inject it as the global config."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        private_key="0x" + "a" * 64,
        _config_dir=tmp_path,
        data_dir=str(data_dir),
    )
    monkeypatch.setattr(config_module, "_config", cfg)
    return cfg

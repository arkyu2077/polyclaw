"""Configuration loader — single source of truth for all settings.

Secrets are loaded from environment variables (or a .env file).
Non-secret strategy parameters can optionally be set in config.yaml.

Environment variables (see .env.example):
  POLYMARKET_PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS
  POLYMARKET_CLOB_API_KEY, POLYMARKET_CLOB_API_SECRET, POLYMARKET_CLOB_API_PASSPHRASE
  DISCORD_WEBHOOK_URL, TWITTER_RAPIDAPI_KEYS, ODDS_API_KEY
  POLYGON_RPC_URL, INITIAL_BANKROLL, DATA_DIR

Optional config.yaml (non-secret strategy params):
  max_order_size, daily_loss_limit, max_positions, max_exposure_pct, min_edge, strategy
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load .env file if present (no-op if missing)
load_dotenv()


@dataclass
class Config:
    """All configuration for the Polymarket News Edge bot."""

    # Required — loaded from env vars
    private_key: str = ""

    # Network
    rpc_url: str = "https://polygon-bor-rpc.publicnode.com"

    # Trading
    bankroll: float = 1000.0
    strategy: str = "sniper"

    # Paths (relative to cwd or absolute)
    data_dir: str = "./data"

    # Safety limits (can be overridden in config.yaml)
    max_order_size: float = 15.0
    daily_loss_limit: float = 30.0
    max_positions: int = 4
    max_exposure_pct: float = 1.0
    min_edge: float = 0.02

    # Notifications — loaded from env vars
    discord_webhook: str = ""

    # Data sources — loaded from env vars
    twitter_rapidapi_keys: list[str] = field(default_factory=list)

    # CLOB API — auto-derived from private_key by setup.sh, loaded from env vars
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""
    wallet_address: str = ""

    # Derived paths (set after loading)
    _config_dir: Path = field(default_factory=Path, repr=False)
    _data_path: Path = field(default_factory=Path, repr=False)

    def __post_init__(self):
        """Resolve relative paths and validate."""
        data_path = Path(self.data_dir)
        if not data_path.is_absolute() and self._config_dir:
            data_path = self._config_dir / data_path
        self._data_path = data_path.resolve()
        self._data_path.mkdir(parents=True, exist_ok=True)

    @property
    def positions_file(self) -> Path:
        return self._data_path / "positions.json"

    @property
    def history_file(self) -> Path:
        return self._data_path / "trade_history.json"

    @property
    def news_cache_file(self) -> Path:
        return self._data_path / "news_feed.json"

    @property
    def market_cache_file(self) -> Path:
        return self._data_path / "market_cache.json"

    @property
    def price_history_file(self) -> Path:
        return self._data_path / "price_history.json"

    @property
    def signals_log_file(self) -> Path:
        return self._data_path / "signals_log.json"

    @property
    def live_positions_file(self) -> Path:
        return self._data_path / "live_positions.json"

    @property
    def live_history_file(self) -> Path:
        return self._data_path / "live_trade_history.json"

    @property
    def arena_dir(self) -> Path:
        d = self._data_path / "arena"
        d.mkdir(exist_ok=True)
        return d

    def validate(self) -> list[str]:
        """Validate config and return list of errors."""
        errors = []

        if not self.private_key or not self.private_key.startswith("0x"):
            errors.append("POLYMARKET_PRIVATE_KEY must be set and start with 0x")

        if self.bankroll <= 0:
            errors.append("bankroll must be positive")

        if self.max_order_size <= 0:
            errors.append("max_order_size must be positive")

        if not (0 < self.min_edge < 1):
            errors.append("min_edge must be between 0 and 1")

        if not (0 < self.max_exposure_pct <= 2):
            errors.append("max_exposure_pct must be between 0 and 2")

        valid_strategies = {"sniper", "baseline", "conservative", "aggressive", "trend_follower"}
        if self.strategy not in valid_strategies:
            errors.append(f"strategy must be one of: {valid_strategies}")

        return errors


# Global config instance
_config: Optional[Config] = None


def _parse_twitter_keys(raw: str) -> list[str]:
    """Parse comma-separated Twitter RapidAPI keys from env var."""
    return [k.strip() for k in raw.split(",") if k.strip()]


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration.

    Secrets always come from environment variables.
    Non-secret strategy params can optionally be overridden via config.yaml.
    """
    global _config

    # --- Non-secret strategy params from optional config.yaml ---
    yaml_data: dict = {}
    if config_path is None:
        # Look for config.yaml in standard locations (optional)
        candidates = [
            Path.cwd() / "config.yaml",
            Path(__file__).parent.parent / "config.yaml",
            Path.home() / ".config" / "polymarket-news-edge" / "config.yaml",
        ]
        env_path = os.environ.get("POLYMARKET_CONFIG")
        if env_path:
            candidates.insert(0, Path(env_path))
        for p in candidates:
            if p.exists():
                config_path = p
                break

    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            yaml_data = yaml.safe_load(f) or {}

    # Non-secret fields allowed from yaml (explicitly whitelisted)
    YAML_ALLOWED = {
        "strategy", "data_dir", "max_order_size", "daily_loss_limit",
        "max_positions", "max_exposure_pct", "min_edge", "bankroll",
    }
    strategy_params = {k: v for k, v in yaml_data.items() if k in YAML_ALLOWED}

    # --- Secrets always from env vars ---
    twitter_keys_raw = os.environ.get("TWITTER_RAPIDAPI_KEYS", "")
    twitter_keys = _parse_twitter_keys(twitter_keys_raw) if twitter_keys_raw else []

    _config = Config(
        # Secrets from env
        private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
        wallet_address=os.environ.get("POLYMARKET_WALLET_ADDRESS", ""),
        clob_api_key=os.environ.get("POLYMARKET_CLOB_API_KEY", ""),
        clob_api_secret=os.environ.get("POLYMARKET_CLOB_API_SECRET", ""),
        clob_api_passphrase=os.environ.get("POLYMARKET_CLOB_API_PASSPHRASE", ""),
        discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        twitter_rapidapi_keys=twitter_keys,
        rpc_url=os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
        bankroll=float(os.environ.get("INITIAL_BANKROLL", strategy_params.pop("bankroll", 1000.0))),
        data_dir=os.environ.get("DATA_DIR", strategy_params.pop("data_dir", "./data")),
        _config_dir=Path(config_path).parent if config_path else Path.cwd(),
        # Non-secret strategy params from yaml (with defaults)
        **strategy_params,
    )

    errors = _config.validate()
    if errors:
        print("Config validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    return _config


def get_config() -> Config:
    """Get the loaded config, or load it if not yet loaded."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def save_config(config: Config, config_path: Path):
    """Save non-secret strategy params back to a YAML file.

    Secrets are NOT written to YAML — they live in env vars / .env only.
    """
    data = {
        "strategy": config.strategy,
        "data_dir": config.data_dir,
        "max_order_size": config.max_order_size,
        "daily_loss_limit": config.daily_loss_limit,
        "max_positions": config.max_positions,
        "max_exposure_pct": config.max_exposure_pct,
        "min_edge": config.min_edge,
    }

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

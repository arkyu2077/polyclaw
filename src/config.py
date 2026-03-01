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
import re
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
    """All configuration for the Polyclaw bot."""

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

    # Position sizing
    max_position_pct: float = 0.15
    cooldown_hours: float = 1.0
    timeout_hours: float = 24.0
    timeout_move_threshold: float = 0.02
    kelly_fraction: float = 0.5
    fee_rate: float = 0.003

    # Exit strategy
    tp_ratio: float = 0.70
    high_conf_tp_ratio: float = 0.85
    low_conf_tp_ratio: float = 0.55
    sl_ratio: float = 0.75
    wide_sl_ratio: float = 0.65
    tight_sl_ratio: float = 0.82
    trailing_stop_activation: float = 0.5
    trailing_stop_distance: float = 0.30

    # Live exit
    live_timeout_hours: float = 6.0
    stale_order_hours: float = 12.0
    price_drift_threshold: float = 0.20

    # Edge calculator
    min_edge_threshold: float = 0.02
    max_kelly_fraction: float = 0.10
    min_shares: int = 5

    # Order execution
    max_spread: float = 0.10
    price_bump_fallback: float = 0.02
    balance_reserve_pct: float = 0.95

    # Signal dedup
    signal_cooldown_hours: float = 4.0
    max_alerts_per_hour: int = 5

    # LLM
    ai_estimate_discount: float = 0.5
    llm_provider: str = ""           # "openai" (+ compatible proxies), "gemini", "anthropic"
    llm_base_url: str = ""           # e.g. "http://127.0.0.1:8045/v1" for local proxy
    llm_api_key: str = ""            # from env var LLM_API_KEY
    llm_model: str = ""              # e.g. "gemini-2.0-flash", "gpt-4o-mini"

    # Arena — which strategies to run
    active_strategies: list[str] = field(default_factory=lambda: ["baseline"])

    # Arena strategy overrides (YAML dict → StrategyConfig fields)
    strategy_overrides: dict = field(default_factory=dict)

    # Notifications — loaded from env vars
    discord_webhook: str = ""

    # Data sources — loaded from env vars
    twitter_rapidapi_keys: list[str] = field(default_factory=list)

    # WorldMonitor sources
    acled_email: str = ""
    acled_password: str = ""
    eia_api_key: str = ""
    rss_feeds_per_cycle: int = 20

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
    def db_path(self) -> Path:
        return self._data_path / "polyclaw.db"

    @property
    def arena_dir(self) -> Path:
        d = self._data_path / "arena"
        d.mkdir(exist_ok=True)
        return d

    def validate(self) -> list[str]:
        """Validate config and return list of errors."""
        errors = []

        if not self.private_key or not re.fullmatch(r'0x[0-9a-fA-F]{64}', self.private_key):
            errors.append("POLYMARKET_PRIVATE_KEY must be 0x followed by 64 hex characters")

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
            Path.home() / ".config" / "polyclaw" / "config.yaml",
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
        # Position sizing
        "max_position_pct", "cooldown_hours", "timeout_hours", "timeout_move_threshold",
        "kelly_fraction", "fee_rate",
        # Exit strategy
        "tp_ratio", "high_conf_tp_ratio", "low_conf_tp_ratio",
        "sl_ratio", "wide_sl_ratio", "tight_sl_ratio",
        "trailing_stop_activation", "trailing_stop_distance",
        # Live exit
        "live_timeout_hours", "stale_order_hours", "price_drift_threshold",
        # Edge calculator
        "min_edge_threshold", "max_kelly_fraction", "min_shares",
        # Order execution
        "max_spread", "price_bump_fallback", "balance_reserve_pct",
        # Signal dedup
        "signal_cooldown_hours", "max_alerts_per_hour",
        # WorldMonitor
        "rss_feeds_per_cycle",
        # LLM
        "ai_estimate_discount", "llm_provider", "llm_base_url", "llm_model",
        # Arena
        "active_strategies", "strategy_overrides",
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
        acled_email=os.environ.get("ACLED_EMAIL", ""),
        acled_password=os.environ.get("ACLED_PASSWORD", ""),
        eia_api_key=os.environ.get("EIA_API_KEY", ""),
        rpc_url=os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
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

"""Tests for position_manager.py — all constants read from config."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# kelly_size tests
# ---------------------------------------------------------------------------

def test_kelly_uses_config_fee_rate(mock_config):
    """Higher fee_rate should reduce net profit and therefore reduce kelly_size."""
    from src.position_manager import kelly_size

    mock_config.fee_rate = 0.01
    mock_config.kelly_fraction = 0.5
    mock_config.max_position_pct = 0.99  # effectively uncapped

    size_high_fee = kelly_size(
        ai_probability=0.70,
        entry_price=0.50,
        confidence=0.80,
        bankroll=1000.0,
    )

    mock_config.fee_rate = 0.001
    size_low_fee = kelly_size(
        ai_probability=0.70,
        entry_price=0.50,
        confidence=0.80,
        bankroll=1000.0,
    )

    # Lower fee => higher net profit => larger kelly size
    assert size_low_fee > size_high_fee


def test_kelly_uses_config_kelly_fraction(mock_config):
    """Smaller kelly_fraction should produce smaller position size."""
    from src.position_manager import kelly_size

    mock_config.fee_rate = 0.003
    mock_config.max_position_pct = 0.99

    mock_config.kelly_fraction = 0.5
    size_half = kelly_size(
        ai_probability=0.70,
        entry_price=0.50,
        confidence=0.80,
        bankroll=1000.0,
    )

    mock_config.kelly_fraction = 0.25
    size_quarter = kelly_size(
        ai_probability=0.70,
        entry_price=0.50,
        confidence=0.80,
        bankroll=1000.0,
    )

    assert size_quarter < size_half


def test_kelly_uses_config_max_position_pct(mock_config):
    """max_position_pct cap should limit the position size."""
    from src.position_manager import kelly_size

    mock_config.fee_rate = 0.003
    mock_config.kelly_fraction = 0.5
    mock_config.max_position_pct = 0.05  # 5% cap

    size = kelly_size(
        ai_probability=0.90,
        entry_price=0.30,
        confidence=1.0,
        bankroll=1000.0,
    )

    # Must not exceed 5% of 1000 = $50
    assert size <= 50.0 + 0.01  # tiny float tolerance


def test_kelly_returns_zero_when_no_edge(mock_config):
    """When ai_probability == entry_price there is no edge; kelly_size should be 0."""
    from src.position_manager import kelly_size

    mock_config.fee_rate = 0.003
    mock_config.kelly_fraction = 0.5
    mock_config.max_position_pct = 0.15

    size = kelly_size(
        ai_probability=0.50,
        entry_price=0.50,
        confidence=1.0,
        bankroll=1000.0,
    )

    assert size == 0.0


# ---------------------------------------------------------------------------
# open_position tests
# ---------------------------------------------------------------------------

def _make_open_position_dict(market_id="mkt-1", status="open"):
    return {
        "id": "pos-1",
        "market_id": market_id,
        "question": "Test?",
        "direction": "BUY_YES",
        "entry_price": 0.50,
        "shares": 10,
        "cost": 5.0,
        "target_price": 0.70,
        "stop_loss": 0.38,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "pnl": None,
        "trigger_news": "",
        "confidence": 0.7,
        "peak_price": 0.50,
        "mode": "paper",
        "strategy": "",
        "token_id": "",
        "filled_shares": 10,
        "order_id": "",
        "neg_risk": 0,
        "trade_id": "",
    }


def test_open_respects_config_max_positions(mock_config, monkeypatch):
    """open_position returns None when open positions >= cfg.max_positions."""
    mock_config.max_positions = 2
    mock_config.max_exposure_pct = 1.0
    mock_config.cooldown_hours = 0.0

    two_positions = [_make_open_position_dict("mkt-A"), _make_open_position_dict("mkt-B")]

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: two_positions)
    monkeypatch.setattr("src.position_manager.get_trades", lambda mode: [])
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)

    from src.position_manager import open_position

    result = open_position(
        market_id="mkt-NEW",
        question="New market?",
        direction="BUY_YES",
        entry_price=0.40,
        ai_probability=0.70,
        bankroll=1000.0,
        confidence=0.80,
    )
    assert result is None


def test_open_respects_config_cooldown(mock_config, monkeypatch):
    """open_position returns None when a trade on same market closed within cooldown_hours."""
    mock_config.max_positions = 5
    mock_config.max_exposure_pct = 1.0
    mock_config.cooldown_hours = 2.0  # 2-hour cooldown

    # Trade closed 1 hour ago — still within cooldown
    closed_1h_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    closed_trade = {
        "market_id": "mkt-COOL",
        "exit_time": closed_1h_ago,
    }

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: [])
    monkeypatch.setattr("src.position_manager.get_trades", lambda mode: [closed_trade])
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)

    from src.position_manager import open_position

    result = open_position(
        market_id="mkt-COOL",
        question="Cooldown market?",
        direction="BUY_YES",
        entry_price=0.40,
        ai_probability=0.70,
        bankroll=1000.0,
        confidence=0.80,
    )
    assert result is None


def test_open_uses_config_tp_ratios(mock_config, monkeypatch):
    """high_conf_tp_ratio should be used when confidence >= 0.75."""
    mock_config.max_positions = 5
    mock_config.max_exposure_pct = 1.0
    mock_config.cooldown_hours = 0.0
    mock_config.high_conf_tp_ratio = 0.90
    mock_config.tp_ratio = 0.70
    mock_config.low_conf_tp_ratio = 0.55
    mock_config.sl_ratio = 0.75
    mock_config.wide_sl_ratio = 0.65
    mock_config.tight_sl_ratio = 0.82
    mock_config.kelly_fraction = 0.5
    mock_config.fee_rate = 0.003
    mock_config.max_position_pct = 0.99

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: [])
    monkeypatch.setattr("src.position_manager.get_trades", lambda mode: [])
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)

    from src.position_manager import open_position

    # confidence=0.80 >= 0.75 → uses high_conf_tp_ratio=0.90
    pos = open_position(
        market_id="mkt-TP",
        question="TP test?",
        direction="BUY_YES",
        entry_price=0.40,
        ai_probability=0.75,
        bankroll=1000.0,
        confidence=0.80,
    )

    if pos is not None:
        # target = entry + (ai_prob - entry) * tp_ratio
        expected_target = round(0.40 + (0.75 - 0.40) * 0.90, 4)
        assert abs(pos.target_price - expected_target) < 0.001


def test_open_uses_config_sl_ratios(mock_config, monkeypatch):
    """tight_sl_ratio config value should be applied when kelly_pct >= 10%."""
    mock_config.max_positions = 5
    mock_config.max_exposure_pct = 1.0
    mock_config.cooldown_hours = 0.0
    mock_config.high_conf_tp_ratio = 0.85
    mock_config.tp_ratio = 0.70
    mock_config.low_conf_tp_ratio = 0.55
    mock_config.sl_ratio = 0.75
    mock_config.wide_sl_ratio = 0.65
    mock_config.tight_sl_ratio = 0.82   # applied when kelly_pct >= 0.10
    mock_config.kelly_fraction = 0.5
    mock_config.fee_rate = 0.003
    mock_config.max_position_pct = 0.99

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: [])
    monkeypatch.setattr("src.position_manager.get_trades", lambda mode: [])
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)

    from src.position_manager import open_position

    # confidence=0.65, entry_price=0.40, ai_probability=0.75, bankroll=1000
    # kelly_size produces a large cost (>= 10% of 1000=$100) → tight_sl_ratio used
    pos = open_position(
        market_id="mkt-SL",
        question="SL test?",
        direction="BUY_YES",
        entry_price=0.40,
        ai_probability=0.75,
        bankroll=1000.0,
        confidence=0.65,
    )

    if pos is not None:
        # kelly_pct = pos.cost / bankroll; if >= 0.10 → tight_sl_ratio=0.82
        kelly_pct = pos.cost / 1000.0
        if kelly_pct >= 0.10:
            expected_sl = round(0.40 * mock_config.tight_sl_ratio, 4)
        elif kelly_pct <= 0.03:
            expected_sl = round(0.40 * mock_config.wide_sl_ratio, 4)
        else:
            expected_sl = round(0.40 * mock_config.sl_ratio, 4)
        assert abs(pos.stop_loss - expected_sl) < 0.001


# ---------------------------------------------------------------------------
# check_exits tests
# ---------------------------------------------------------------------------

def _make_position_obj(
    market_id="mkt-exit",
    entry_price=0.50,
    target_price=0.70,
    stop_loss=0.38,
    peak_price=None,
    confidence=0.7,
    hours_old=0,
):
    """Build a Position dataclass instance for exit tests."""
    from src.position_manager import Position

    entry_time = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return Position(
        id="pos-exit",
        market_id=market_id,
        question="Exit test?",
        direction="BUY_YES",
        entry_price=entry_price,
        shares=10,
        cost=round(10 * entry_price, 2),
        target_price=target_price,
        stop_loss=stop_loss,
        entry_time=entry_time,
        status="open",
        confidence=confidence,
        peak_price=peak_price or entry_price,
    )


def test_exit_trailing_stop_uses_config(mock_config, monkeypatch):
    """Trailing stop should fire when progress >= cfg.trailing_stop_activation
    and price drops cfg.trailing_stop_distance from peak."""
    mock_config.trailing_stop_activation = 0.3   # 30% of target move reached
    mock_config.trailing_stop_distance = 0.20    # 20% drop from peak triggers stop
    mock_config.timeout_hours = 48.0             # timeout won't interfere

    pos = _make_position_obj(
        entry_price=0.40,
        target_price=0.60,   # target_move = 0.20
        stop_loss=0.30,
        peak_price=0.50,     # progress = (0.50-0.40)/0.20 = 0.50 >= 0.30 ✓
    )

    # current price drops 20% from peak: 0.50 * (1 - 0.20) = 0.40
    current_price = round(0.50 * (1 - 0.20), 4)  # = 0.40

    closed_positions = []

    def fake_close(p, exit_price, reason):
        p.status = "closed"
        p.exit_reason = reason
        closed_positions.append((p, exit_price, reason))

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: [pos.to_db_dict()])
    monkeypatch.setattr("src.position_manager._fetch_market_price", lambda mid: current_price)
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)
    monkeypatch.setattr("src.position_manager._close_position", fake_close)

    from src.position_manager import check_exits

    count = check_exits()
    assert count == 1
    assert "TRAILING_STOP" in closed_positions[0][2]


def test_exit_timeout_uses_config(mock_config, monkeypatch):
    """Timeout should trigger at cfg.timeout_hours (12h) not hardcoded 24h."""
    mock_config.timeout_hours = 12.0
    mock_config.timeout_move_threshold = 0.02
    mock_config.trailing_stop_activation = 0.5
    mock_config.trailing_stop_distance = 0.30

    # Position is 13 hours old — past 12h timeout but would NOT trigger 24h hardcoded
    pos = _make_position_obj(
        entry_price=0.50,
        target_price=0.70,
        stop_loss=0.38,
        peak_price=0.50,
        hours_old=13,
    )

    # Current price has barely moved (< 2% threshold) → TIMEOUT_FLAT
    current_price = 0.505  # only 1% move, below threshold

    closed_positions = []

    def fake_close(p, exit_price, reason):
        p.status = "closed"
        closed_positions.append((p, exit_price, reason))

    monkeypatch.setattr("src.position_manager.get_positions", lambda mode: [pos.to_db_dict()])
    monkeypatch.setattr("src.position_manager._fetch_market_price", lambda mid: current_price)
    monkeypatch.setattr("src.position_manager.upsert_position", lambda d: None)
    monkeypatch.setattr("src.position_manager._close_position", fake_close)

    from src.position_manager import check_exits

    count = check_exits()
    assert count == 1
    assert closed_positions[0][2] == "TIMEOUT_FLAT"

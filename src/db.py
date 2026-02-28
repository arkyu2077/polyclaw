"""SQLite database layer — single module owning all DB access.

Provides atomic transactions via Python's built-in sqlite3 (zero new deps).
WAL mode enables concurrent reads during writes.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import get_config

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """Get a thread-local singleton connection with WAL mode."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    db_path = get_config().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection | None = None):
    """Create tables if they don't exist."""
    if conn is None:
        conn = get_db()
    conn.executescript(_SCHEMA)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    mode TEXT NOT NULL,
    strategy TEXT DEFAULT '',
    market_id TEXT NOT NULL,
    token_id TEXT DEFAULT '',
    question TEXT,
    direction TEXT,
    entry_price REAL,
    shares INTEGER,
    filled_shares INTEGER DEFAULT 0,
    cost REAL,
    target_price REAL,
    stop_loss REAL,
    confidence REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    order_id TEXT DEFAULT '',
    entry_time TEXT,
    exit_price REAL,
    exit_time TEXT,
    exit_reason TEXT,
    pnl REAL,
    trigger_news TEXT DEFAULT '',
    neg_risk INTEGER DEFAULT 0,
    peak_price REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT DEFAULT '',
    market_id TEXT,
    question TEXT,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    shares INTEGER,
    cost REAL,
    pnl REAL,
    fees REAL DEFAULT 0,
    entry_time TEXT,
    exit_time TEXT,
    exit_reason TEXT,
    trigger_news TEXT,
    confidence REAL,
    hold_hours REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_id TEXT,
    question TEXT,
    direction TEXT,
    current_price REAL,
    ai_probability REAL,
    edge REAL,
    raw_edge REAL,
    fee_estimate REAL,
    confidence REAL,
    position_size REAL,
    reliability TEXT,
    news_titles TEXT,
    llm_reasoning TEXT,
    filter_reason TEXT,
    cooldown_age_hours REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signal_cooldowns (
    key TEXT PRIMARY KEY,
    last_alert TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    action TEXT,
    consumed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    trade_id TEXT PRIMARY KEY,
    status TEXT,
    market_id TEXT,
    question TEXT,
    direction TEXT,
    signal_data TEXT,
    decision_data TEXT,
    order_data TEXT,
    fill_data TEXT,
    settlement_data TEXT,
    events TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


# ═══════════════════════════════════════════════════
# Positions CRUD
# ═══════════════════════════════════════════════════

def get_positions(mode: str = "live", strategy: str = "", status: str | None = None) -> list[dict]:
    """Get positions filtered by mode, strategy, and optional status."""
    conn = get_db()
    sql = "SELECT * FROM positions WHERE mode = ?"
    params: list = [mode]
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def upsert_position(pos: dict):
    """Insert or update a position row."""
    conn = get_db()
    cols = [
        "id", "trade_id", "mode", "strategy", "market_id", "token_id",
        "question", "direction", "entry_price", "shares", "filled_shares",
        "cost", "target_price", "stop_loss", "confidence", "status",
        "order_id", "entry_time", "exit_price", "exit_time", "exit_reason",
        "pnl", "trigger_news", "neg_risk", "peak_price",
    ]
    vals = [pos.get(c) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO positions ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        vals,
    )
    conn.commit()


def delete_position(position_id: str):
    conn = get_db()
    conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
    conn.commit()


def delete_positions_by_status(mode: str, status: str, strategy: str = ""):
    """Delete positions matching mode+status (e.g. remove cancelled)."""
    conn = get_db()
    sql = "DELETE FROM positions WHERE mode = ? AND status = ?"
    params: list = [mode, status]
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    conn.execute(sql, params)
    conn.commit()


# ═══════════════════════════════════════════════════
# Trades (history) CRUD
# ═══════════════════════════════════════════════════

def insert_trade(trade: dict):
    """Insert a closed trade into the history table."""
    conn = get_db()
    cols = [
        "position_id", "mode", "strategy", "market_id", "question",
        "direction", "entry_price", "exit_price", "shares", "cost",
        "pnl", "fees", "entry_time", "exit_time", "exit_reason",
        "trigger_news", "confidence", "hold_hours",
    ]
    vals = [trade.get(c) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO trades ({col_names}) VALUES ({placeholders})", vals)
    conn.commit()


def get_trades(mode: str = "live", strategy: str = "", limit: int = 200) -> list[dict]:
    conn = get_db()
    sql = "SELECT * FROM trades WHERE mode = ?"
    params: list = [mode]
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_daily_pnl(mode: str = "live") -> float:
    """Sum of PnL for trades closed today."""
    conn = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE mode = ? AND exit_time LIKE ?",
        (mode, f"{today}%"),
    ).fetchone()
    return float(row["total"]) if row else 0.0


# ═══════════════════════════════════════════════════
# Signals CRUD
# ═══════════════════════════════════════════════════

def insert_signal(sig: dict):
    """Insert a signal log entry."""
    conn = get_db()
    cols = [
        "timestamp", "market_id", "question", "direction", "current_price",
        "ai_probability", "edge", "raw_edge", "fee_estimate", "confidence",
        "position_size", "reliability", "news_titles", "llm_reasoning",
        "filter_reason", "cooldown_age_hours",
    ]
    vals = []
    for c in cols:
        v = sig.get(c)
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        vals.append(v)
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO signals ({col_names}) VALUES ({placeholders})", vals)
    conn.commit()


def get_signals(limit: int = 100) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════
# Signal cooldowns CRUD
# ═══════════════════════════════════════════════════

def get_cooldown(key: str) -> str | None:
    conn = get_db()
    row = conn.execute("SELECT last_alert FROM signal_cooldowns WHERE key = ?", (key,)).fetchone()
    return row["last_alert"] if row else None


def set_cooldown(key: str, ts: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO signal_cooldowns (key, last_alert) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET last_alert = excluded.last_alert",
        (key, ts),
    )
    conn.commit()


def prune_cooldowns(cutoff_ts: float):
    """Delete cooldowns older than cutoff (unix timestamp)."""
    conn = get_db()
    # We store ISO timestamps, so convert and compare
    conn.execute(
        "DELETE FROM signal_cooldowns WHERE strftime('%s', last_alert) < ?",
        (str(int(cutoff_ts)),),
    )
    conn.commit()


def get_all_cooldowns() -> dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT key, last_alert FROM signal_cooldowns").fetchall()
    return {r["key"]: r["last_alert"] for r in rows}


# ═══════════════════════════════════════════════════
# Notifications CRUD
# ═══════════════════════════════════════════════════

def add_notification(message: str, action: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO notifications (message, action) VALUES (?, ?)",
        (message, action),
    )
    conn.commit()


def get_pending_notifications() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE consumed = 0 ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_consumed(ids: list[int]):
    if not ids:
        return
    ids = [int(i) for i in ids]  # enforce integer type
    conn = get_db()
    placeholders = ", ".join(["?"] * len(ids))
    conn.execute(f"UPDATE notifications SET consumed = 1 WHERE id IN ({placeholders})", ids)
    conn.commit()


# ═══════════════════════════════════════════════════
# Decisions CRUD
# ═══════════════════════════════════════════════════

def upsert_decision(dec: dict):
    """Insert or update a decision record."""
    conn = get_db()
    # Serialize JSON fields
    for field in ("signal_data", "decision_data", "order_data", "fill_data", "settlement_data", "events"):
        if field in dec and not isinstance(dec[field], str):
            dec[field] = json.dumps(dec[field], ensure_ascii=False) if dec[field] is not None else None

    cols = [
        "trade_id", "status", "market_id", "question", "direction",
        "signal_data", "decision_data", "order_data", "fill_data",
        "settlement_data", "events",
    ]
    vals = [dec.get(c) for c in cols]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "trade_id")
    updates += ", updated_at = datetime('now')"
    conn.execute(
        f"INSERT INTO decisions ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(trade_id) DO UPDATE SET {updates}",
        vals,
    )
    conn.commit()


def get_decision(trade_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM decisions WHERE trade_id = ?", (trade_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    # Deserialize JSON fields
    for field in ("signal_data", "decision_data", "order_data", "fill_data", "settlement_data", "events"):
        if d.get(field) and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def get_decisions(status: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        for field in ("signal_data", "decision_data", "order_data", "fill_data", "settlement_data", "events"):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        results.append(d)
    return results

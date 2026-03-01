"""Generate trading report from Polyclaw DB (not on-chain history)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams

from .config import get_config
from .db import get_db


def _wallet_balance() -> float | None:
    """Fetch on-chain USDC.e balance."""
    cfg = get_config()
    addr = cfg.wallet_address.lower()
    try:
        r = httpx.post("https://1rpc.io/matic", json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                        "data": "0x70a08231000000000000000000000000" + addr[2:]}, "latest"]
        }, timeout=10)
        return int(r.json()["result"], 16) / 1e6
    except Exception:
        return None


def _pending_orders() -> tuple[int, float]:
    """Get count and locked value of pending CLOB orders."""
    cfg = get_config()
    try:
        creds = ApiCreds(
            api_key=cfg.clob_api_key,
            api_secret=cfg.clob_api_secret,
            api_passphrase=cfg.clob_api_passphrase,
        )
        client = ClobClient(
            "https://clob.polymarket.com",
            key=cfg.private_key,
            chain_id=137,
            creds=creds,
            signature_type=0,
        )
        orders = client.get_orders()
        locked = sum(float(o["original_size"]) * float(o["price"]) for o in orders)
        return len(orders), locked
    except Exception:
        return 0, 0.0


def generate_report() -> str:
    """Generate report from Polyclaw DB positions only."""
    cfg = get_config()
    now = datetime.now(timezone.utc)
    db = get_db()

    # Wallet
    usdc_e = _wallet_balance()
    order_count, order_locked = _pending_orders()

    # Open positions from DB
    open_rows = db.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY created_at DESC"
    ).fetchall()

    # Closed positions from DB
    closed_rows = db.execute(
        "SELECT * FROM positions WHERE status='closed' ORDER BY created_at DESC"
    ).fetchall()

    # Build report
    lines = []
    total_open_cost = 0.0

    for row in open_rows:
        r = dict(row)
        q = r["question"][:45]
        mode = r["mode"]
        direction = r["direction"]
        shares = r["shares"]
        entry = r["entry_price"]
        cost = r["cost"]
        total_open_cost += cost
        tag = f"[{mode}]" if mode != "live" else ""
        lines.append(f"🟡 {q} | {direction} {shares}股 @${entry:.3f} | 成本${cost:.2f} {tag}")

    # Closed stats
    wins = losses = 0
    total_pnl = 0.0
    today_pnl = 0.0
    today_str = now.strftime("%Y-%m-%d")

    for row in closed_rows:
        r = dict(row)
        pnl = r.get("pnl") or 0.0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        # Today's trades
        exit_time = r.get("exit_time", "") or ""
        if today_str in exit_time:
            today_pnl += pnl

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # Scanner status
    status_file = Path(cfg.data_dir) / "status.json"
    scanner_status = ""
    if status_file.exists():
        try:
            s = json.loads(status_file.read_text())
            pid = s.get("pid", "?")
            hb = s.get("last_heartbeat", "?")
            errors = s.get("consecutive_errors", 0)
            scanner_status = f"🤖 Scanner: ✅ PID {pid} | 心跳: {hb} | 错误: {errors}"
        except Exception:
            scanner_status = "🤖 Scanner: ⚠️ 状态未知"

    # Format report
    report = f"📊 **Polyclaw 报告** ({now.strftime('%m/%d %H:%M')} UTC)\n\n"

    if usdc_e is not None:
        report += f"💰 钱包USDC.e: ${usdc_e:.2f}\n"
    report += f"📋 {order_count}笔挂单 (锁定~${order_locked:.0f})\n\n"

    if lines:
        report += "**活跃仓位:**\n"
        for line in lines:
            report += f"{line}\n"
        report += f"\n持仓成本: ${total_open_cost:.2f}\n"
    else:
        report += "📭 无活跃仓位\n"

    report += f"\n**已平仓: {total_trades}笔 | 胜{wins}/负{losses} | 胜率{win_rate:.0f}%**\n"
    report += f"已实现PnL: **${total_pnl:+.2f}**\n"

    if today_pnl != 0:
        report += f"今日PnL: ${today_pnl:+.2f}\n"

    report += f"\n{scanner_status}"

    return report


if __name__ == "__main__":
    print(generate_report())

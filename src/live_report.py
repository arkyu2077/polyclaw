"""Generate live trading position report with P&L from on-chain data."""

import json
import os
import httpx
from pathlib import Path
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams

from config import get_config
from db import get_positions


def _get_client():
    cfg = get_config()
    creds = ApiCreds(
        api_key=cfg.clob_api_key,
        api_secret=cfg.clob_api_secret,
        api_passphrase=cfg.clob_api_passphrase,
    )
    return ClobClient(
        "https://clob.polymarket.com",
        key=cfg.private_key,
        chain_id=137,
        creds=creds,
        signature_type=0,
    ), cfg


def generate_report() -> str:
    """Generate a complete live trading report."""
    client, cfg = _get_client()
    now = datetime.now(timezone.utc)

    # 1. CLOB balance
    bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=0)
    )
    clob_balance = int(bal["balance"]) / 1e6

    # 2. On-chain USDC.e balance
    addr = cfg.wallet_address.lower()
    try:
        r = httpx.post("https://1rpc.io/matic", json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                        "data": "0x70a08231000000000000000000000000" + addr[2:]}, "latest"]
        }, timeout=10)
        onchain_usdc_e = int(r.json()["result"], 16) / 1e6
    except Exception:
        onchain_usdc_e = None

    # 3. Get all trades and compute positions
    trades = client.get_trades()
    our_addr = cfg.wallet_address

    # Aggregate positions by condition_id + outcome
    positions = {}
    for t in trades:
        market = t["market"]
        if t["trader_side"] == "TAKER":
            key = f"{market}:{t['outcome']}"
            if key not in positions:
                positions[key] = {"market": market, "outcome": t["outcome"],
                                  "shares": 0, "cost": 0, "fees": 0}
            positions[key]["shares"] += float(t["size"])
            cost = float(t["size"]) * float(t["price"])
            positions[key]["cost"] += cost
            positions[key]["fees"] += cost * int(t["fee_rate_bps"]) / 10000
        else:
            for mo in t["maker_orders"]:
                if mo["maker_address"] == our_addr:
                    key = f"{market}:{mo['outcome']}"
                    if key not in positions:
                        positions[key] = {"market": market, "outcome": mo["outcome"],
                                          "shares": 0, "cost": 0, "fees": 0}
                    positions[key]["shares"] += float(mo["matched_amount"])
                    cost = float(mo["matched_amount"]) * float(mo["price"])
                    positions[key]["cost"] += cost
                    positions[key]["fees"] += cost * int(mo["fee_rate_bps"]) / 10000

    # 4. Get current prices and compute P&L
    lines = []
    total_cost = 0
    total_value = 0
    total_pnl = 0

    for key, pos in positions.items():
        market_id = pos["market"]
        try:
            mdata = client.get_market(market_id)
            question = mdata.get("question", "?")[:45]
            tokens = mdata.get("tokens", [])

            current_price = None
            for t in tokens:
                if t["outcome"] == pos["outcome"]:
                    current_price = float(t.get("price", 0))
                    break

            if current_price is None:
                continue

            value = pos["shares"] * current_price
            pnl = value - pos["cost"] - pos["fees"]
            total_cost += pos["cost"] + pos["fees"]
            total_value += value
            total_pnl += pnl

            icon = "✅" if current_price == 1 else ("❌" if current_price == 0 else ("🟢" if pnl >= 0 else "🔴"))
            status = "已结算" if current_price in (0, 1) else f"@${current_price:.3f}"

            lines.append(f"{icon} {question} | {pos['outcome']} {pos['shares']:.1f}股 {status} | ${pnl:+.2f}")
        except Exception:
            continue

    # 5. Pending orders
    orders = client.get_orders()
    order_locked = sum(float(o["original_size"]) * float(o["price"]) for o in orders)

    # 6. Build report
    report = f"📊 **实盘报告** ({now.strftime('%m/%d %H:%M')} UTC)\n"
    if onchain_usdc_e is not None:
        report += f"💰 钱包USDC.e: ${onchain_usdc_e:.2f}\n"
    report += f"📋 {len(orders)}笔挂单 (锁定~${order_locked:.0f})\n\n"

    if lines:
        report += "**持仓:**\n"
        for line in lines:
            report += f"{line}\n"
        report += f"\n投入: ${total_cost:.2f} | 市值: ${total_value:.2f} | **盈亏: ${total_pnl:+.2f}**\n"

    # 7. Bot live positions (from db)
    bot_open = get_positions(mode="live", status="open")
    if bot_open:
        report += f"\n🤖 Bot仓位: {len(bot_open)}个活跃\n"

    # Total assets = initial bankroll + total P&L from all trades
    initial = cfg.bankroll
    total_assets = initial + total_pnl
    report += f"\n**总资产: ${total_assets:.2f} | 本金${initial:.2f} | 总盈亏: ${total_pnl:+.2f} ({total_pnl/initial*100:+.1f}%)**"

    return report


if __name__ == "__main__":
    print(generate_report())

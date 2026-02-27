"""Redemption — on-chain CTF token redemption for resolved markets."""

from datetime import datetime, timezone

from rich.console import Console

from config import get_config
from db import add_notification
from order_executor import _get_client

console = Console()


def auto_redeem_resolved() -> float:
    """Check for resolved markets with CTF tokens and redeem them.
    Returns total USDC.e redeemed."""
    import httpx
    from web3 import Web3

    cfg = get_config()

    # Find RPCs
    for rpc in [cfg.rpc_url, 'https://polygon.meowrpc.com']:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                break
        except Exception:
            continue
    else:
        console.print("[red]  ❌ No Polygon RPC available for redeem[/red]")
        return 0.0

    account = w3.to_checksum_address(cfg.wallet_address)
    collateral = w3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
    ctf_addr = w3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')

    ctf_abi = [{
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"
    }]
    balance_abi = [{'inputs':[{'name':'','type':'address'},{'name':'','type':'uint256'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'stateMutability':'view','type':'function'}]

    ctf = w3.eth.contract(address=ctf_addr, abi=ctf_abi)
    ctf_balance = w3.eth.contract(address=ctf_addr, abi=balance_abi)

    client = _get_client()
    trades = client.get_trades()

    redeemed_total = 0.0
    seen_conditions = set()

    for trade in trades:
        condition_id = trade.get('market', '')
        if condition_id in seen_conditions:
            continue
        seen_conditions.add(condition_id)

        try:
            import httpx as _httpx
            r = _httpx.get(f'https://clob.polymarket.com/markets/{condition_id}', timeout=10)
            mdata = r.json()
            tokens = mdata.get('tokens', [])

            resolved = any(float(t.get('price', 0.5)) in (0, 1) for t in tokens)
            if not resolved:
                continue

            has_tokens = False
            for t in tokens:
                token_id = int(t.get('token_id', '0'))
                if token_id == 0:
                    continue
                bal = ctf_balance.functions.balanceOf(account, token_id).call()
                if bal > 0:
                    has_tokens = True
                    break

            if not has_tokens:
                continue

            console.print(f"[cyan]  🔄 Redeeming: {mdata.get('question', '?')[:50]}[/cyan]")

            cid_bytes = bytes.fromhex(condition_id[2:]) if condition_id.startswith('0x') else bytes.fromhex(condition_id)
            nonce = w3.eth.get_transaction_count(account, 'pending')

            tx = ctf.functions.redeemPositions(
                collateral, b'\x00' * 32, cid_bytes, [1, 2]
            ).build_transaction({
                'from': account, 'nonce': nonce, 'gas': 300000,
                'maxFeePerGas': w3.to_wei(100, 'gwei'),
                'maxPriorityFeePerGas': w3.to_wei(50, 'gwei'),
                'chainId': 137
            })

            signed = w3.eth.account.sign_transaction(tx, cfg.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt['status'] == 1:
                console.print(f"[green]  ✅ Redeemed! tx={tx_hash.hex()[:16]}...[/green]")
                redeemed_total += 1
                add_notification(
                    f"🔄 赎回成功: {mdata.get('question','?')[:50]} → USDC.e已回到钱包\ntx: {tx_hash.hex()[:20]}...",
                    "REDEEM",
                )
            else:
                console.print(f"[red]  ❌ Redeem failed for {condition_id[:20]}[/red]")

        except Exception as e:
            console.print(f"[yellow]  ⚠ Redeem check error: {e}[/yellow]")
            continue

    return redeemed_total

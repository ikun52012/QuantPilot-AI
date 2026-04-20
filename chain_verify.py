"""
Best-effort on-chain payment verification for submitted crypto payments.
"""
import os
from decimal import Decimal

import httpx


USDT_CONTRACTS = {
    "ERC20": {"USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7", "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"},
    "BEP20": {"USDT": "0x55d398326f99059ff775485246999027b3197955", "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"},
    "ARBITRUM": {"USDT": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831"},
}

EVM_SCAN_APIS = {
    "ERC20": ("ETHERSCAN_API_URL", "https://api.etherscan.io/api", "ETHERSCAN_API_KEY"),
    "BEP20": ("BSCSCAN_API_URL", "https://api.bscscan.com/api", "BSCSCAN_API_KEY"),
    "ARBITRUM": ("ARBISCAN_API_URL", "https://api.arbiscan.io/api", "ARBISCAN_API_KEY"),
}

TOKEN_DECIMALS = {"USDT": 6, "USDC": 6}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _addr(value: str) -> str:
    return (value or "").lower().replace("0x", "")


def _amount_from_hex(data: str, decimals: int) -> Decimal:
    data = data or "0x0"
    return Decimal(int(data, 16)) / (Decimal(10) ** decimals)


async def verify_payment_tx(
    network: str,
    tx_hash: str,
    expected_address: str,
    expected_amount: float,
    currency: str = "USDT",
) -> dict:
    """Return verification status for a payment tx hash."""
    network = (network or "").upper().strip()
    currency = (currency or "USDT").upper().strip()
    tx_hash = (tx_hash or "").strip()
    if not tx_hash:
        return {"verified": False, "status": "missing_tx", "reason": "No transaction hash submitted"}

    if network in EVM_SCAN_APIS:
        return await _verify_evm_token_transfer(network, tx_hash, expected_address, expected_amount, currency)
    if network == "TRC20":
        return await _verify_tron_transfer(tx_hash, expected_address, expected_amount, currency)
    if network == "APT":
        return await _verify_aptos_transfer(tx_hash, expected_address, expected_amount, currency)
    return {"verified": False, "status": "manual_required", "reason": f"No verifier configured for {network}"}


async def _verify_evm_token_transfer(network: str, tx_hash: str, expected_address: str, expected_amount: float, currency: str) -> dict:
    contract = USDT_CONTRACTS.get(network, {}).get(currency)
    if not contract:
        return {"verified": False, "status": "manual_required", "reason": f"{currency} contract is not configured for {network}"}
    api_url_env, default_url, api_key_env = EVM_SCAN_APIS[network]
    api_url = os.getenv(api_url_env, default_url)
    api_key = os.getenv(api_key_env, "")
    params = {
        "module": "proxy",
        "action": "eth_getTransactionReceipt",
        "txhash": tx_hash,
    }
    if api_key:
        params["apikey"] = api_key
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(api_url, params=params)
        resp.raise_for_status()
        data = resp.json()
    receipt = data.get("result")
    if not receipt:
        return {"verified": False, "status": "not_found", "reason": "Transaction receipt was not found"}
    if str(receipt.get("status", "")).lower() not in {"0x1", "1"}:
        return {"verified": False, "status": "failed", "reason": "Transaction receipt is not successful"}

    expected_to = _addr(expected_address)
    expected_contract = _addr(contract)
    required = Decimal(str(expected_amount or 0))
    tolerance = Decimal("0.000001")
    matches = []
    for log in receipt.get("logs", []):
        topics = [str(t).lower() for t in log.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        if _addr(log.get("address", "")) != expected_contract:
            continue
        to_addr = topics[2][-40:]
        amount = _amount_from_hex(log.get("data", "0x0"), TOKEN_DECIMALS.get(currency, 6))
        if to_addr == expected_to:
            matches.append({"amount": str(amount), "to": "0x" + to_addr})
            if amount + tolerance >= required:
                return {
                    "verified": True,
                    "status": "verified",
                    "reason": f"{currency} transfer matched {network} receipt",
                    "amount": str(amount),
                    "matches": matches,
                }
    return {
        "verified": False,
        "status": "mismatch",
        "reason": "No matching token transfer to the configured address was found",
        "matches": matches,
    }


async def _verify_tron_transfer(tx_hash: str, expected_address: str, expected_amount: float, currency: str) -> dict:
    api_url = os.getenv("TRONGRID_API_URL", "https://api.trongrid.io")
    api_key = os.getenv("TRONGRID_API_KEY", "")
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    url = f"{api_url.rstrip('/')}/v1/transactions/{tx_hash}/events"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    required = Decimal(str(expected_amount or 0))
    expected = (expected_address or "").strip()
    matches = []
    for event in data.get("data", []):
        result = event.get("result") or {}
        to_addr = result.get("to") or result.get("_to") or ""
        raw_value = result.get("value") or result.get("_value")
        if not raw_value or to_addr != expected:
            continue
        amount = Decimal(str(raw_value)) / Decimal(10) ** TOKEN_DECIMALS.get(currency, 6)
        matches.append({"amount": str(amount), "to": to_addr})
        if amount >= required:
            return {"verified": True, "status": "verified", "reason": "TRC20 transfer matched", "amount": str(amount), "matches": matches}
    return {"verified": False, "status": "mismatch", "reason": "No matching TRC20 transfer event was found", "matches": matches}


async def _verify_aptos_transfer(tx_hash: str, expected_address: str, expected_amount: float, currency: str) -> dict:
    api_url = os.getenv("APTOS_API_URL", "https://fullnode.mainnet.aptoslabs.com/v1")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{api_url.rstrip('/')}/transactions/by_hash/{tx_hash}")
        resp.raise_for_status()
        data = resp.json()
    if not data or data.get("success") is False:
        return {"verified": False, "status": "failed", "reason": "Aptos transaction is not successful"}
    return {
        "verified": False,
        "status": "manual_required",
        "reason": "Aptos transaction exists, but token transfer parsing requires a configured indexer workflow",
        "tx_version": data.get("version"),
        "expected_address": expected_address,
        "expected_amount": expected_amount,
        "currency": currency,
    }

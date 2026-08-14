"""
Signal Server - Chain Verification
Verify blockchain transactions for payment confirmation.
"""
import os
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import httpx
from loguru import logger

# Block explorer APIs
EXPLORER_APIS = {
    "TRC20": {
        "url": "https://apilist.tronscanapi.com/api/transaction",
        "param": "value",
    },
    "ERC20": {
        "url": "https://api.etherscan.io/api",
        "key_env": "ETHERSCAN_API_KEY",
    },
    "BEP20": {
        "url": "https://api.bscscan.com/api",
        "key_env": "BSCSCAN_API_KEY",
    },
    "ARBITRUM": {
        "url": "https://api.arbiscan.io/api",
        "key_env": "ARBISCAN_API_KEY",
    },
    "APT": {
        "url": "",
        "manual": True,
    },
    "SOL": {
        "url": "https://api.mainnet-beta.solana.com",
    },
}

EVM_USDT_CONTRACTS = {
    "ERC20": {"0xdac17f958d2ee523a2206206994597c13d831ec7"},
    "BEP20": {"0x55d398326f99059ff775485246999027b3197955"},
    "ARBITRUM": {"0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"},
}

# P0-FIX: Canonical TRON USDT contract address
TRC20_USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"


async def verify_payment_tx(
    tx_hash: str,
    network: str,
    expected_amount: float | None = None,
    expected_address: str | None = None,
) -> dict:
    """
    Verify a blockchain transaction.

    Returns verification result with status and details.
    """
    network = network.upper()

    if network not in EXPLORER_APIS:
        return {
            "verified": False,
            "status": "unsupported",
            "reason": f"Network {network} not supported for auto-verification",
        }

    try:
        if network == "TRC20":
            return await _verify_trc20(tx_hash, expected_amount, expected_address)
        elif network in ("ERC20", "BEP20", "ARBITRUM"):
            return await _verify_evm(tx_hash, network, expected_amount, expected_address)
        elif network == "SOL":
            return await _verify_solana(tx_hash, expected_amount, expected_address)
        elif network == "APT":
            return {
                "verified": False,
                "status": "manual_review",
                "reason": "Aptos transactions require manual verification",
            }
        else:
            return {
                "verified": False,
                "status": "unsupported",
                "reason": f"No verifier for {network}",
            }
    except Exception as e:
        logger.error(f"[ChainVerify] Error verifying {tx_hash}: {e}")
        return {
            "verified": False,
            "status": "error",
            "reason": "Verification failed due to an internal error",
        }


async def _verify_trc20(
    tx_hash: str,
    expected_amount: float | None,
    expected_address: str | None,
) -> dict:
    """Verify TRC20 transaction."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://apilist.tronscanapi.com/api/transaction-info",
            params={"value": tx_hash},
        )

        if resp.status_code != 200:
            return {"verified": False, "status": "error", "reason": "API error"}

        data = resp.json()

        if not data:
            return {"verified": False, "status": "not_found"}

        # Check confirmation status
        confirmed = data.get("confirmed", False)
        if not confirmed:
            return {
                "verified": False,
                "status": "pending",
                "confirmations": data.get("confirmations", 0),
            }

        # Extract transfer info
        transfers = data.get("trc20TransferInfo", [])
        if not transfers:
            transfers = data.get("tokenTransferInfo", [])

        for transfer in transfers:
            to_address = transfer.get("to_address", "")
            amount_str = transfer.get("amount_str", "0")
            # P0-FIX: Verify TRC20 USDT contract address
            contract_address = transfer.get("contract_address", "").lower()
            if not contract_address or contract_address != TRC20_USDT_CONTRACT.lower():
                continue

            try:
                amount_int = int(amount_str)  # USDT has 6 decimals on TRC20
                amount = float(Decimal(amount_int) / Decimal(1_000_000))
            except (TypeError, ValueError, InvalidOperation):
                amount = 0

            if expected_address and to_address.lower() != expected_address.lower():
                continue

            if expected_amount:
                try:
                    amt_dec = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    exp_dec = Decimal(str(expected_amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    if abs(amt_dec - exp_dec) > Decimal("0.01"):
                        continue
                except (InvalidOperation, ValueError):
                    if abs(amount - expected_amount) > 0.01:
                        continue

            return {
                "verified": True,
                "status": "confirmed",
                "amount": amount,
                "to_address": to_address,
                "from_address": transfer.get("from_address", ""),
                "timestamp": data.get("timestamp"),
            }

        return {"verified": False, "status": "no_matching_transfer"}


async def _verify_evm(
    tx_hash: str,
    network: str,
    expected_amount: float | None,
    expected_address: str | None,
) -> dict:
    """Verify EVM USDT token transfer details, not just transaction success."""
    config = EXPLORER_APIS.get(network, {})
    if not isinstance(config, dict):
        config = {}
    base_url = str(config.get("url", ""))
    key_env = str(config.get("key_env", ""))
    api_key = os.getenv(key_env, "")

    if not expected_address or not expected_amount:
        return {
            "verified": False,
            "status": "manual_review",
            "reason": "Expected amount/address required for EVM auto-verification",
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        receipt_params = {
            "module": "transaction",
            "action": "gettxreceiptstatus",
            "txhash": tx_hash,
        }

        if api_key:
            receipt_params["apikey"] = api_key

        resp = await client.get(base_url, params=receipt_params)

        if resp.status_code != 200:
            return {"verified": False, "status": "error", "reason": "API error"}

        data = resp.json()
        result_data = data.get("result", {})
        if not isinstance(result_data, dict):
            return {"verified": False, "status": "error", "reason": "Unexpected API response format"}
        status = result_data.get("status", "0")

        if status == "0":
            return {"verified": False, "status": "failed"}
        if status != "1":
            return {"verified": False, "status": "pending"}

        token_params = {
            "module": "account",
            "action": "tokentx",
            "txhash": tx_hash,
        }
        if api_key:
            token_params["apikey"] = api_key

        token_resp = await client.get(base_url, params=token_params)
        if token_resp.status_code != 200:
            return {"verified": False, "status": "error", "reason": "Token transfer API error"}

        token_data = token_resp.json()
        transfers = token_data.get("result") or []
        if not isinstance(transfers, list):
            return {
                "verified": False,
                "status": "manual_review",
                "reason": "Explorer did not return token transfer details",
            }

        expected_contracts = EVM_USDT_CONTRACTS.get(network, set())
        expected_to = expected_address.lower().strip()
        min_amount = max(0.0, float(expected_amount) - 0.01)

        for transfer in transfers:
            contract = str(transfer.get("contractAddress") or "").lower()
            if expected_contracts and contract not in expected_contracts:
                continue
            to_address = str(transfer.get("to") or "").lower()
            if to_address != expected_to:
                continue
            try:
                decimals = int(transfer.get("tokenDecimal") or 6)
                amount = int(str(transfer.get("value") or "0")) / (10 ** decimals)
            except (TypeError, ValueError, OverflowError):
                continue
            if amount < min_amount:
                continue
            return {
                "verified": True,
                "status": "confirmed",
                "amount": amount,
                "to_address": transfer.get("to", ""),
                "from_address": transfer.get("from", ""),
                "token": transfer.get("tokenSymbol", "USDT"),
                "block_number": transfer.get("blockNumber"),
            }

        return {
            "verified": False,
            "status": "no_matching_transfer",
            "reason": "No USDT transfer matched expected address and amount",
        }


async def _verify_solana(
    tx_hash: str,
    expected_amount: float | None,
    expected_address: str | None,
) -> dict:
    """Verify Solana transaction with amount and address validation.

    C4-FIX: Now verifies:
    - Transaction is finalized
    - Amount matches expected amount (within 0.01 tolerance)
    - Recipient address matches expected address
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[tx_hash]],
            },
        )

        if resp.status_code != 200:
            return {"verified": False, "status": "error", "reason": "API error"}

        data = resp.json()
        result = data.get("result", {}).get("value", [])

        if not result or not result[0]:
            return {"verified": False, "status": "not_found"}

        status = result[0]

        if status.get("err"):
            return {"verified": False, "status": "failed", "error": status.get("err")}

        if status.get("confirmationStatus") != "finalized":
            return {
                "verified": False,
                "status": "pending",
                "confirmations": status.get("confirmations", 0),
            }

        if not expected_amount or not expected_address:
            return {
                "verified": False,
                "status": "manual_review",
                "slot": status.get("slot"),
                "reason": "Amount and address verification required for Solana auto-verification",
            }

        tx_resp = await client.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [tx_hash, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            },
        )

        if tx_resp.status_code != 200:
            return {"verified": False, "status": "error", "reason": "Failed to fetch transaction details"}

        tx_data = tx_resp.json()
        tx_result = tx_data.get("result", {})
        if not tx_result:
            return {"verified": False, "status": "error", "reason": "Transaction details unavailable"}

        meta = tx_result.get("meta", {})
        if meta.get("err"):
            return {"verified": False, "status": "failed", "error": meta.get("err")}

        # USDT on Solana is an SPL token, not native SOL.
        # Check postTokenBalances / preTokenBalances for SPL token transfers.
        # Solana USDT mint address: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
        SOL_USDT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        USDT_DECIMALS = 6

        post_token_balances = meta.get("postTokenBalances") or []
        pre_token_balances = meta.get("preTokenBalances") or []

        expected_address_lower = expected_address.lower().strip()
        amount_found = False
        amount_received = 0.0

        # Build index: (accountIndex) -> pre token balance
        pre_token_map: dict[int, dict] = {}
        for entry in pre_token_balances:
            if isinstance(entry, dict) and entry.get("mint") == SOL_USDT_MINT:
                idx = int(entry.get("accountIndex", -1))
                pre_token_map[idx] = entry

        # Check post token balances for USDT received at expected address
        for entry in post_token_balances:
            if not isinstance(entry, dict):
                continue
            if entry.get("mint") != SOL_USDT_MINT:
                continue
            owner = str(entry.get("owner") or "").lower().strip()
            if owner != expected_address_lower:
                continue
            try:
                post_amount = int(entry.get("uiTokenAmount", {}).get("amount", "0"))
            except (ValueError, TypeError):
                continue
            idx = int(entry.get("accountIndex", -1))
            pre_entry = pre_token_map.get(idx, {})
            try:
                pre_amount = int(pre_entry.get("uiTokenAmount", {}).get("amount", "0"))
            except (ValueError, TypeError):
                pre_amount = 0
            received_raw = post_amount - pre_amount
            amount_received = received_raw / (10 ** USDT_DECIMALS)
            try:
                recv_dec = Decimal(str(amount_received)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                exp_dec = Decimal(str(expected_amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                if abs(recv_dec - exp_dec) <= Decimal("0.01"):
                    amount_found = True
            except (InvalidOperation, ValueError):
                if abs(amount_received - expected_amount) <= 0.01:
                    amount_found = True
            break

        if not amount_found:
            return {
                "verified": False,
                "status": "amount_mismatch",
                "expected": expected_amount,
                "received": amount_received,
                "reason": "Payment amount or recipient address does not match",
            }

        return {
            "verified": True,
            "status": "confirmed",
            "slot": status.get("slot"),
            "amount": amount_received,
        }

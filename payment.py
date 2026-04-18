"""
TradingView Signal Server - Crypto Payment Module
Handles USDT/USDC payment address generation and verification.
"""
import os
import hashlib
import time
from loguru import logger
from database import get_admin_setting, set_admin_setting

# ─────────────────────────────────────────────
# Supported payment networks
# ─────────────────────────────────────────────
SUPPORTED_NETWORKS = {
    "TRC20": {
        "name": "Tron (TRC20)",
        "currencies": ["USDT", "USDC"],
        "confirmation_time": "~3 minutes",
        "fee": "~1 USDT",
    },
    "ERC20": {
        "name": "Ethereum (ERC20)",
        "currencies": ["USDT", "USDC"],
        "confirmation_time": "~5 minutes",
        "fee": "~5-20 USDT (gas)",
    },
    "BEP20": {
        "name": "BSC (BEP20)",
        "currencies": ["USDT", "USDC"],
        "confirmation_time": "~3 minutes",
        "fee": "~0.5 USDT",
    },
    "SOL": {
        "name": "Solana (SPL)",
        "currencies": ["USDT", "USDC"],
        "confirmation_time": "~1 minute",
        "fee": "~0.01 USDT",
    },
    "BTC": {
        "name": "Bitcoin",
        "currencies": ["BTC"],
        "confirmation_time": "~30 minutes",
        "fee": "variable",
    },
}


def get_payment_address(network: str = "TRC20") -> str:
    """
    Get the admin's payment receiving address for the specified network.
    Addresses are stored in admin_settings.
    """
    network = network.upper().strip()
    if network not in SUPPORTED_NETWORKS:
        return ""
    key = f"payment_address_{network}"
    addr = get_admin_setting(key, "")

    # Fall back to environment variables
    if not addr:
        env_key = f"PAYMENT_ADDRESS_{network}"
        addr = os.getenv(env_key, "")

    return addr


def set_payment_address(network: str, address: str):
    """Set the admin's payment address for a network."""
    network = network.upper().strip()
    address = address.strip()
    if network not in SUPPORTED_NETWORKS:
        raise ValueError(f"Unsupported payment network: {network}")
    if not address:
        raise ValueError("Payment address cannot be empty")
    key = f"payment_address_{network}"
    set_admin_setting(key, address)
    logger.info(f"[Payment] Updated {network} address: {address[:8]}...{address[-6:]}")


def get_all_payment_addresses() -> dict:
    """Get all configured payment addresses."""
    result = {}
    for network in SUPPORTED_NETWORKS:
        addr = get_payment_address(network)
        if addr:
            result[network] = {
                "address": addr,
                "network_name": SUPPORTED_NETWORKS[network]["name"],
                "currencies": SUPPORTED_NETWORKS[network]["currencies"],
            }
    return result


def generate_payment_memo(user_id: str, payment_id: str) -> str:
    """
    Generate a unique payment memo/tag for identification.
    This helps match incoming payments to users.
    """
    raw = f"{user_id}:{payment_id}:{int(time.time())}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12].upper()


def create_payment_request(user_id: str, plan_price: float, currency: str = "USDT",
                            network: str = "TRC20") -> dict:
    """
    Create a payment request with address and memo.
    Returns payment details for the user.
    """
    network = network.upper().strip()
    currency = currency.upper().strip()
    network_info = SUPPORTED_NETWORKS.get(network)
    if not network_info:
        return {"error": f"Unsupported payment network: {network}"}
    if currency not in network_info["currencies"]:
        return {"error": f"{currency} is not supported on {network}"}

    address = get_payment_address(network)
    if not address:
        return {"error": f"No payment address configured for {network}"}

    return {
        "address": address,
        "amount": plan_price,
        "currency": currency,
        "network": network,
        "network_name": network_info.get("name", network),
        "confirmation_time": network_info.get("confirmation_time", "varies"),
        "fee_estimate": network_info.get("fee", "varies"),
        "instructions": [
            f"Send exactly {plan_price} {currency} to the address below",
            f"Network: {network_info.get('name', network)}",
            "After sending, submit the transaction hash (TX ID)",
            "Your subscription will be activated after admin confirmation",
        ]
    }


def get_supported_payment_options() -> list[dict]:
    """Return list of supported payment networks and currencies."""
    result = []
    for network_id, info in SUPPORTED_NETWORKS.items():
        addr = get_payment_address(network_id)
        if addr:  # Only show networks with configured addresses
            result.append({
                "network": network_id,
                "name": info["name"],
                "currencies": info["currencies"],
                "confirmation_time": info["confirmation_time"],
                "fee": info["fee"],
                "available": True,
            })
    return result

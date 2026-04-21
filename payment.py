"""
Signal Server - Payment Module (Enhanced)
Crypto payment handling with multi-chain support.
"""
import json
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.database import AdminSettingModel


# Supported payment networks
SUPPORTED_NETWORKS = {
    "TRC20": {"name": "Tron (TRC20)", "currency": "USDT", "confirmations": 20},
    "ERC20": {"name": "Ethereum (ERC20)", "currency": "USDT", "confirmations": 12},
    "BEP20": {"name": "BSC (BEP20)", "currency": "USDT", "confirmations": 12},
    "ARBITRUM": {"name": "Arbitrum One", "currency": "USDT", "confirmations": 12},
    "SOL": {"name": "Solana (SPL)", "currency": "USDT", "confirmations": 32},
    "APT": {"name": "Aptos (APT)", "currency": "USDT", "confirmations": 1},
}


async def get_payment_address(
    session: AsyncSession,
    currency: str,
    network: str,
) -> Optional[str]:
    """Get payment wallet address for a currency/network."""
    key = f"payment_address_{currency}_{network}".upper()

    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == key)
    )
    setting = result.scalar_one_or_none()

    if setting:
        return setting.value

    # Fallback to legacy key format
    legacy_key = f"payment_address_{network}".lower()
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == legacy_key)
    )
    setting = result.scalar_one_or_none()

    return setting.value if setting else None


async def set_payment_address(
    session: AsyncSession,
    currency: str,
    network: str,
    address: str,
) -> bool:
    """Set payment wallet address."""
    key = f"payment_address_{currency}_{network}".upper()

    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == key)
    )
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = address
    else:
        setting = AdminSettingModel(key=key, value=address)
        session.add(setting)

    await session.commit()
    return True


async def get_all_payment_addresses(session: AsyncSession) -> dict:
    """Get all configured payment addresses."""
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key.like("payment_address_%"))
    )
    settings = result.scalars().all()

    addresses = {}
    for setting in settings:
        # Parse key: payment_address_CURRENCY_NETWORK
        parts = setting.key.replace("payment_address_", "").split("_")
        if len(parts) >= 2:
            network = parts[-1]
            currency = "_".join(parts[:-1])
            addresses[f"{currency}_{network}"] = {
                "currency": currency,
                "network": network,
                "address": setting.value,
            }

    return addresses


def get_supported_payment_options() -> list[dict]:
    """Get list of supported payment options."""
    return [
        {
            "network": network,
            "name": info["name"],
            "currency": info["currency"],
            "confirmations": info["confirmations"],
        }
        for network, info in SUPPORTED_NETWORKS.items()
    ]


async def create_payment_request(
    session: AsyncSession,
    amount: float,
    currency: str,
    network: str,
    user_id: str,
    subscription_id: Optional[str] = None,
) -> dict:
    """Create a payment request."""
    from datetime import datetime, timezone, timedelta
    from core.database import PaymentModel
    import uuid

    address = await get_payment_address(session, currency, network)
    if not address:
        raise ValueError(f"No payment address configured for {currency}/{network}")

    payment = PaymentModel(
        id=str(uuid.uuid4()),
        user_id=user_id,
        subscription_id=subscription_id,
        amount=amount,
        currency=currency,
        network=network,
        wallet_address=address,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    session.add(payment)
    await session.commit()

    return {
        "payment_id": payment.id,
        "amount": payment.amount,
        "currency": payment.currency,
        "network": payment.network,
        "wallet_address": payment.wallet_address,
        "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
    }

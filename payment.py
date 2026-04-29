"""
Signal Server - Payment Module (Enhanced)
Crypto payment handling with multi-chain support.
"""
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


def _setting_value_text(setting: AdminSettingModel | None) -> str | None:
    value = cast(Any, getattr(setting, "value", None))
    return str(value) if value else None


async def get_payment_address(
    session: AsyncSession,
    currency: str,
    network: str,
) -> str | None:
    """Get payment wallet address for a currency/network."""
    key = f"payment_address_{currency}_{network}".upper()

    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == key)
    )
    setting = result.scalar_one_or_none()

    if setting:
        return _setting_value_text(setting)

    # Fallback to legacy key format
    legacy_key = f"payment_address_{network}".lower()
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == legacy_key)
    )
    setting = result.scalar_one_or_none()

    return _setting_value_text(setting)


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
        cast(Any, setting).value = address
    else:
        setting = AdminSettingModel(key=key, value=address)
        session.add(setting)

    await session.commit()
    return True


async def get_all_payment_addresses(session: AsyncSession) -> dict[str, dict[str, str | None]]:
    """Get all configured payment addresses."""
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key.like("payment_address_%"))
    )
    settings = result.scalars().all()

    addresses: dict[str, dict[str, str | None]] = {}
    for setting in settings:
        # Parse key: payment_address_CURRENCY_NETWORK
        key_text = str(cast(Any, getattr(setting, "key", "")))
        parts = key_text.replace("payment_address_", "").split("_")
        if len(parts) >= 2:
            network = parts[-1]
            currency = "_".join(parts[:-1])
            addresses[f"{currency}_{network}"] = {
                "currency": currency,
                "network": network,
                "address": _setting_value_text(setting),
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
    subscription_id: str | None = None,
) -> dict:
    """Create a payment request."""
    import uuid
    from datetime import timedelta

    from core.database import PaymentModel
    from core.utils.datetime import utcnow

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
        expires_at=utcnow() + timedelta(hours=24),
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

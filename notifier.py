"""
OpenClaw Signal Server - Telegram Notifications
Sends trading notifications to Telegram.
"""
import httpx
from loguru import logger
from config import settings
from models import TradeDecision, AIAnalysis


async def send_telegram(text: str):
    """Send a message to Telegram."""
    if not settings.telegram.bot_token or not settings.telegram.chat_id:
        logger.debug("[Telegram] Not configured, skipping notification")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.telegram.bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            logger.debug("[Telegram] Message sent")
    except Exception as e:
        logger.error(f"[Telegram] Failed to send message: {e}")


async def notify_signal_received(ticker: str, direction: str, price: float):
    """Notify when a new signal is received."""
    text = (
        f"📡 <b>Signal Received</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Price: <code>{price}</code>\n"
        f"Status: Analyzing..."
    )
    await send_telegram(text)


async def notify_pre_filter_blocked(ticker: str, direction: str, reason: str):
    """Notify when a signal is blocked by pre-filter."""
    text = (
        f"🚫 <b>Signal Blocked (Pre-Filter)</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Direction: {direction.upper()}\n"
        f"Reason: {reason}"
    )
    await send_telegram(text)


async def notify_ai_analysis(ticker: str, analysis: AIAnalysis):
    """Notify AI analysis results."""
    emoji_map = {
        "execute": "✅",
        "modify": "🔄",
        "reject": "❌",
        "hold": "⏸️",
    }
    emoji = emoji_map.get(analysis.recommendation, "❓")

    warnings_text = ""
    if analysis.warnings:
        warnings_text = "\n⚠️ Warnings:\n" + "\n".join(f"  • {w}" for w in analysis.warnings)

    text = (
        f"{emoji} <b>AI Analysis: {analysis.recommendation.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Confidence: <b>{analysis.confidence:.0%}</b>\n"
        f"Risk Score: {analysis.risk_score:.0%}\n"
        f"Market: {analysis.market_condition}\n"
        f"Position Size: {analysis.position_size_pct:.0%}\n"
        f"\n💬 {analysis.reasoning}"
        f"{warnings_text}"
    )
    await send_telegram(text)


async def notify_trade_executed(decision: TradeDecision, order_result: dict):
    """Notify when a trade is executed."""
    status = order_result.get("status", "unknown")
    if status in ("filled", "simulated"):
        mode = "📝 PAPER" if status == "simulated" else "💰 LIVE"
        text = (
            f"🎯 <b>Trade Executed</b> ({mode})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Ticker: <code>{decision.ticker}</code>\n"
            f"Direction: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"Entry: <code>{decision.entry_price}</code>\n"
            f"Stop Loss: <code>{decision.stop_loss}</code>\n"
            f"Take Profit: <code>{decision.take_profit}</code>\n"
            f"Quantity: <code>{decision.quantity}</code>\n"
            f"\n🤖 AI Confidence: {decision.ai_analysis.confidence:.0%}" if decision.ai_analysis else ""
        )
    elif status == "error":
        text = (
            f"💥 <b>Trade Failed</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Ticker: <code>{decision.ticker}</code>\n"
            f"Error: {order_result.get('reason', 'Unknown')}"
        )
    else:
        text = f"ℹ️ Trade status: {status} - {order_result.get('reason', '')}"

    await send_telegram(text)


async def notify_error(error: str):
    """Notify about system errors."""
    text = f"🔴 <b>System Error</b>\n━━━━━━━━━━━━━━━━━━\n{error}"
    await send_telegram(text)


async def notify_daily_summary(trades: int, win_rate: float, pnl: float):
    """Send daily trading summary."""
    emoji = "📈" if pnl >= 0 else "📉"
    text = (
        f"{emoji} <b>Daily Summary</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total Trades: {trades}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"P&L: <b>{pnl:+.2f}%</b>"
    )
    await send_telegram(text)

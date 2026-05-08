"""
QuantPilot AI - Telegram Notifications
Sends trading notifications to Telegram with i18n support.
Includes retry mechanism for network resilience.
"""
import asyncio
import html

import httpx
from loguru import logger

from core.config import settings
from models import AIAnalysis, TradeDecision

_TELEGRAM_MAX_RETRIES = 2
_TELEGRAM_RETRY_DELAY_SECS = 2.0


def _safe_html(text: str) -> str:
    """Escape text for safe embedding in Telegram HTML messages."""
    return html.escape(str(text))


def _lang() -> str:
    """Get the configured notification language."""
    return getattr(settings, "notification_language", "en") or "en"


_PHRASES = {
    "en": {
        "signal_received": "📡 <b>Signal Received</b>",
        "signal_blocked": "🚫 <b>Signal Blocked (Pre-Filter)</b>",
        "signal_batched": "📦 <b>Signal Batched (Duplicate Detection)</b>",
        "signal_queued": "⏳ <b>Signal Queued</b>",
        "ai_analysis": "AI Analysis",
        "trade_executed": "🎯 <b>Trade Executed</b>",
        "trade_failed": "💥 <b>Trade Failed</b>",
        "trade_rejected": "🚫 <b>Trade Rejected</b>",
        "trade_blocked": "⏸️ <b>Trade Blocked</b>",
        "execution_blocked": "🛡️ <b>Execution Blocked</b>",
        "system_error": "🔴 <b>System Error</b>",
        "daily_summary": "Daily Summary",
        "ticker": "Ticker",
        "direction": "Direction",
        "price": "Price",
        "status": "Status",
        "reason": "Reason",
        "confidence": "Confidence",
        "risk_score": "Risk Score",
        "market": "Market",
        "position_size": "Position Size",
        "entry": "Entry",
        "stop_loss": "Stop Loss",
        "take_profit": "Take Profit",
        "quantity": "Quantity",
        "total_trades": "Total Trades",
        "win_rate": "Win Rate",
        "pnl": "P&L",
        "warnings": "Warnings",
        "analyzing": "Analyzing...",
        "error": "Error",
        "paper": "PAPER",
        "live": "LIVE",
    },
    "zh": {
        "signal_received": "📡 <b>收到信号</b>",
        "signal_blocked": "🚫 <b>信号被拦截（预过滤）</b>",
        "signal_batched": "📦 <b>信号被批量处理（重复检测）</b>",
        "signal_queued": "⏳ <b>信号已排队</b>",
        "ai_analysis": "AI 分析",
        "trade_executed": "🎯 <b>交易已执行</b>",
        "trade_failed": "💥 <b>交易失败</b>",
        "trade_rejected": "🚫 <b>交易被拒绝</b>",
        "trade_blocked": "⏸️ <b>交易被拦截</b>",
        "execution_blocked": "🛡️ <b>执行被拦截</b>",
        "system_error": "🔴 <b>系统错误</b>",
        "daily_summary": "每日总结",
        "ticker": "交易对",
        "direction": "方向",
        "price": "价格",
        "status": "状态",
        "reason": "原因",
        "confidence": "置信度",
        "risk_score": "风险评分",
        "market": "市场",
        "position_size": "仓位大小",
        "entry": "入场",
        "stop_loss": "止损",
        "take_profit": "止盈",
        "quantity": "数量",
        "total_trades": "总交易数",
        "win_rate": "胜率",
        "pnl": "盈亏",
        "warnings": "警告",
        "analyzing": "分析中...",
        "error": "错误",
        "paper": "模拟",
        "live": "实盘",
    },
}


def _format_take_profit_text(decision: TradeDecision) -> str:
    levels = list(decision.take_profit_levels or [])
    if levels:
        return ", ".join(str(tp.price) for tp in levels if getattr(tp, "price", None))
    return str(decision.take_profit)


def _t(key: str) -> str:
    lang = _lang()
    return _PHRASES.get(lang, _PHRASES["en"]).get(key, _PHRASES["en"].get(key, key))


async def send_telegram(text: str):
    """Send a message to Telegram with retry mechanism."""
    if not settings.telegram.bot_token or not settings.telegram.chat_id:
        logger.debug("[Telegram] Not configured, skipping notification")
        return

    for attempt in range(_TELEGRAM_MAX_RETRIES + 1):
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
                return
        except Exception as e:
            if attempt < _TELEGRAM_MAX_RETRIES:
                logger.warning(f"[Telegram] Send failed (attempt {attempt + 1}), retrying: {e}")
                await asyncio.sleep(_TELEGRAM_RETRY_DELAY_SECS)
            else:
                logger.error(f"[Telegram] Failed to send message after {_TELEGRAM_MAX_RETRIES + 1} attempts: {e}")


async def notify_signal_received(ticker: str, direction: str, price: float):
    """Notify when a new signal is received."""
    text = (
        f"{_t('signal_received')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{_safe_html(ticker)}</code>\n"
        f"{_t('direction')}: <b>{_safe_html(direction).upper()}</b>\n"
        f"{_t('price')}: <code>{price}</code>\n"
        f"{_t('status')}: {_t('analyzing')}"
    )
    await send_telegram(text)


async def notify_pre_filter_blocked(ticker: str, direction: str, reason: str):
    """Notify when a signal is blocked by pre-filter."""
    text = (
        f"{_t('signal_blocked')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{_safe_html(ticker)}</code>\n"
        f"{_t('direction')}: {_safe_html(direction).upper()}\n"
        f"{_t('reason')}: {_safe_html(reason)}"
    )
    await send_telegram(text)


async def notify_signal_batched(ticker: str, direction: str, batch_count: int, window_secs: int):
    """Notify when a signal is batched with duplicates."""
    text = (
        f"{_t('signal_batched')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{_safe_html(ticker)}</code>\n"
        f"{_t('direction')}: {_safe_html(direction).upper()}\n"
        f"📊 Batch count: {batch_count}\n"
        f"⏱️ Window: {window_secs}s\n"
        f"💡 Reason: Too many same-direction signals within window"
    )
    await send_telegram(text)


async def notify_signal_queued(ticker: str, direction: str, reason: str):
    """Notify when a signal is queued/rejected due to queue limit."""
    text = (
        f"{_t('signal_queued')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{_safe_html(ticker)}</code>\n"
        f"{_t('direction')}: {_safe_html(direction).upper()}\n"
        f"{_t('reason')}: {_safe_html(reason)}"
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
        warnings_text = f"\n⚠️ {_t('warnings')}:\n" + "\n".join(f"  • {_safe_html(w)}" for w in analysis.warnings)

    tp_levels = [
        value
        for value in [analysis.suggested_tp1, analysis.suggested_tp2, analysis.suggested_tp3, analysis.suggested_tp4]
        if value
    ]
    tp_text = f"\n{_t('take_profit')}: <code>{', '.join(str(v) for v in tp_levels)}</code>" if tp_levels else ""

    text = (
        f"{emoji} <b>{_t('ai_analysis')}: {_safe_html(analysis.recommendation).upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{_safe_html(ticker)}</code>\n"
        f"{_t('confidence')}: <b>{analysis.confidence:.0%}</b>\n"
        f"{_t('risk_score')}: {analysis.risk_score:.0%}\n"
        f"{_t('market')}: {_safe_html(analysis.market_condition)}\n"
        f"{_t('position_size')}: {analysis.position_size_pct:.0%}\n"
        f"{tp_text}"
        f"\n💬 {_safe_html(analysis.reasoning)}"
        f"{warnings_text}"
    )
    await send_telegram(text)


async def notify_trade_executed(decision: TradeDecision, order_result: dict):
    """Notify when a trade is executed."""
    status = order_result.get("status", "unknown")
    if status in ("filled", "simulated"):
        mode = f"📝 {_t('paper')}" if status == "simulated" else f"💰 {_t('live')}"
        text = (
            f"{_t('trade_executed')} ({mode})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_t('ticker')}: <code>{decision.ticker}</code>\n"
            f"{_t('direction')}: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"{_t('entry')}: <code>{decision.entry_price}</code>\n"
            f"{_t('stop_loss')}: <code>{decision.stop_loss}</code>\n"
            f"{_t('take_profit')}: <code>{_format_take_profit_text(decision)}</code>\n"
            f"{_t('quantity')}: <code>{decision.quantity}</code>"
        )
        if decision.ai_analysis:
            text += f"\n🤖 {_t('confidence')}: {decision.ai_analysis.confidence:.0%}"
    elif status == "error":
        text = (
            f"{_t('trade_failed')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_t('ticker')}: <code>{decision.ticker}</code>\n"
            f"{_t('direction')}: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"{_t('error')}: {order_result.get('reason', 'Unknown')}"
        )
        if decision.ai_analysis:
            text += f"\n🤖 {_t('confidence')}: {decision.ai_analysis.confidence:.0%}"
    elif status == "rejected":
        text = (
            f"{_t('trade_rejected')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_t('ticker')}: <code>{decision.ticker}</code>\n"
            f"{_t('direction')}: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"{_t('reason')}: {order_result.get('reason', 'Unknown rejection reason')}"
        )
        if decision.ai_analysis:
            text += f"\n🤖 {_t('confidence')}: {decision.ai_analysis.confidence:.0%}"
        trading_control = order_result.get("trading_control", {})
        if trading_control:
            text += f"\n🛡️ Block: {trading_control.get('block_reason', 'System control')}"
    elif status == "blocked":
        text = (
            f"{_t('execution_blocked')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_t('ticker')}: <code>{decision.ticker}</code>\n"
            f"{_t('direction')}: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"{_t('reason')}: {order_result.get('reason', 'Execution blocked')}"
        )
        if decision.ai_analysis:
            text += f"\n🤖 {_t('confidence')}: {decision.ai_analysis.confidence:.0%}"
    else:
        text = (
            f"ℹ️ {_t('status')}: {status}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_t('ticker')}: <code>{decision.ticker}</code>\n"
            f"{_t('reason')}: {order_result.get('reason', '')}"
        )

    await send_telegram(text)


async def notify_error(error: str):
    """Notify about system errors."""
    text = f"{_t('system_error')}\n━━━━━━━━━━━━━━━━━━\n{error}"
    await send_telegram(text)


async def notify_daily_summary(trades: int, win_rate: float, pnl: float):
    """Send daily trading summary."""
    emoji = "📈" if pnl >= 0 else "📉"
    text = (
        f"{emoji} <b>{_t('daily_summary')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('total_trades')}: {trades}\n"
        f"{_t('win_rate')}: {win_rate:.1f}%\n"
        f"{_t('pnl')}: <b>{pnl:+.2f}%</b>"
    )
    await send_telegram(text)


async def notify_subscription_expired(user_id: str):
    """Notify user when their subscription expires."""
    text = (
        "⚠️ <b>Subscription Expired</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Your trading subscription has expired.\n"
        "• Live trading has been disabled\n"
        "• Existing positions will continue to be monitored\n"
        "• New signals will be processed in paper mode only\n\n"
        "Please renew your subscription to restore live trading."
    )
    await send_telegram(text)

"""
QuantPilot AI - Telegram Notifications
Sends trading notifications to Telegram with i18n support.
"""
import httpx
from loguru import logger

from core.config import settings
from models import AIAnalysis, TradeDecision


def _lang() -> str:
    """Get the configured notification language."""
    return getattr(settings, "notification_language", "en") or "en"


_PHRASES = {
    "en": {
        "signal_received": "📡 <b>Signal Received</b>",
        "signal_blocked": "🚫 <b>Signal Blocked (Pre-Filter)</b>",
        "ai_analysis": "AI Analysis",
        "trade_executed": "🎯 <b>Trade Executed</b>",
        "trade_failed": "💥 <b>Trade Failed</b>",
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
        "ai_analysis": "AI 分析",
        "trade_executed": "🎯 <b>交易已执行</b>",
        "trade_failed": "💥 <b>交易失败</b>",
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
        f"{_t('signal_received')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{ticker}</code>\n"
        f"{_t('direction')}: <b>{direction.upper()}</b>\n"
        f"{_t('price')}: <code>{price}</code>\n"
        f"{_t('status')}: {_t('analyzing')}"
    )
    await send_telegram(text)


async def notify_pre_filter_blocked(ticker: str, direction: str, reason: str):
    """Notify when a signal is blocked by pre-filter."""
    text = (
        f"{_t('signal_blocked')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{ticker}</code>\n"
        f"{_t('direction')}: {direction.upper()}\n"
        f"{_t('reason')}: {reason}"
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
        warnings_text = f"\n⚠️ {_t('warnings')}:\n" + "\n".join(f"  • {w}" for w in analysis.warnings)

    tp_levels = [
        value
        for value in [analysis.suggested_tp1, analysis.suggested_tp2, analysis.suggested_tp3, analysis.suggested_tp4]
        if value
    ]
    tp_text = f"\n{_t('take_profit')}: <code>{', '.join(str(v) for v in tp_levels)}</code>" if tp_levels else ""

    text = (
        f"{emoji} <b>{_t('ai_analysis')}: {analysis.recommendation.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{_t('ticker')}: <code>{ticker}</code>\n"
        f"{_t('confidence')}: <b>{analysis.confidence:.0%}</b>\n"
        f"{_t('risk_score')}: {analysis.risk_score:.0%}\n"
        f"{_t('market')}: {analysis.market_condition}\n"
        f"{_t('position_size')}: {analysis.position_size_pct:.0%}\n"
        f"{tp_text}"
        f"\n💬 {analysis.reasoning}"
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
            f"{_t('error')}: {order_result.get('reason', 'Unknown')}"
        )
    else:
        text = f"ℹ️ {_t('status')}: {status} - {order_result.get('reason', '')}"

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

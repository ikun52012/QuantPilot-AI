"""Helpers for converting scanner candidates into internal signals."""
from __future__ import annotations

import json
from typing import Any

from core.config import settings
from models import MarketContext, SignalDirection, SignalSource, TradingViewSignal


def _get(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _direction(value: Any) -> SignalDirection:
    text = str(value or "long").lower().strip()
    return SignalDirection.SHORT if text == "short" else SignalDirection.LONG


def _compact_json(payload: dict[str, Any], max_len: int = 1800) -> str:
    text = json.dumps(payload, ensure_ascii=True, default=str, separators=(",", ":"))
    if len(text) <= max_len:
        return text
    payload = dict(payload)
    payload["reasons"] = list(payload.get("reasons") or [])[:4]
    payload["smc"] = {
        key: value
        for key, value in (payload.get("smc") or {}).items()
        if key in {"timeframe", "trend", "zone", "support_type", "support_midpoint"}
    }
    text = json.dumps(payload, ensure_ascii=True, default=str, separators=(",", ":"))
    return text[:max_len]


def build_synthetic_signal(candidate: Any, *, secret: str | None = None) -> tuple[TradingViewSignal, dict[str, Any]]:
    """Build a TradingViewSignal-compatible object plus raw audit payload."""
    direction = _direction(_get(candidate, "direction"))
    ticker = str(_get(candidate, "exchange_symbol") or _get(candidate, "watch_symbol") or "").upper().strip()
    price = float(_get(candidate, "current_price") or _get(candidate, "entry_reference") or 0.0)
    timeframe = str(_get(candidate, "timeframe") or "1h")
    setup_hash = str(_get(candidate, "setup_hash") or "")

    scanner_payload = {
        "source": SignalSource.AUTO_SCANNER.value,
        "signal_source": SignalSource.AUTO_SCANNER.value,
        "strategy": "AI_Auto_Scanner",
        "mode": settings.scanner.mode,
        "watch_symbol": _get(candidate, "watch_symbol"),
        "exchange_symbol": ticker,
        "exchange_name": str(_get(candidate, "exchange_name") or settings.exchange.name).lower(),
        "market_type": str(_get(candidate, "market_type") or settings.exchange.market_type).lower(),
        "mapped_asset": bool(_get(candidate, "mapped_asset", False)),
        "data_source": _get(candidate, "data_source"),
        "direction": direction.value,
        "timeframe": timeframe,
        "score": round(float(_get(candidate, "score") or 0.0), 2),
        "setup_type": _get(candidate, "setup_type"),
        "price_zone": _get(candidate, "price_zone"),
        "setup_hash": setup_hash,
        "reasons": list(_get(candidate, "reasons", []) or []),
        "indicators": _get(candidate, "indicator_summary", {}) or {},
        "smc": _get(candidate, "smc_summary", {}) or {},
        "quality": _get(candidate, "quality", {}) or {},
    }
    message = _compact_json(scanner_payload)

    signal = TradingViewSignal(
        secret=secret or "",
        ticker=ticker,
        exchange=str(_get(candidate, "exchange_name") or settings.exchange.name).upper(),
        direction=direction,
        price=price,
        timeframe=timeframe,
        strategy="AI_Auto_Scanner",
        message=message,
    )

    raw_body = {
        "alert_id": setup_hash or f"auto_scanner:{ticker}:{direction.value}:{timeframe}",
        "secret": secret or "",
        "ticker": ticker,
        "exchange": signal.exchange,
        "direction": direction.value,
        "price": price,
        "timeframe": timeframe,
        "strategy": signal.strategy,
        "message": message,
        "signal_source": SignalSource.AUTO_SCANNER.value,
        "scanner": scanner_payload,
    }
    return signal, raw_body


def _tf_seconds(timeframe: str) -> int:
    text = str(timeframe or "1h").lower().strip()
    if text.endswith("m"):
        return max(60, int(text[:-1] or 1) * 60)
    if text.endswith("h"):
        return max(60, int(text[:-1] or 1) * 3600)
    if text.endswith("d"):
        return max(60, int(text[:-1] or 1) * 86400)
    try:
        return max(60, int(text) * 60)
    except ValueError:
        return 3600


def _change_pct(candles: list[Any], current: float, seconds: int, timeframe: str) -> float:
    if current <= 0 or not candles:
        return 0.0
    step = max(1, round(seconds / _tf_seconds(timeframe)))
    if len(candles) <= step:
        ref = float(getattr(candles[0], "close", 0.0) or 0.0)
    else:
        ref = float(getattr(candles[-step - 1], "close", 0.0) or 0.0)
    return ((current - ref) / ref * 100.0) if ref > 0 else 0.0


def market_context_from_bundle(bundle: Any, *, ticker: str | None = None) -> MarketContext:
    """Convert a scanner OHLCV bundle into the existing AI market context model."""
    configured_tfs = list(settings.scanner.timeframes or ["1h"])
    primary_tf = str(getattr(bundle, "primary_timeframe", "") or configured_tfs[0] or "1h")
    candles_by_tf = getattr(bundle, "candles", {}) or {}
    candles = list(candles_by_tf.get(primary_tf) or [])
    if not candles and candles_by_tf:
        primary_tf, candles = next(iter(candles_by_tf.items()))
        candles = list(candles or [])

    current = float(getattr(bundle, "current_price", 0.0) or 0.0)
    recent = candles[-96:] if candles else []
    highs = [float(getattr(c, "high", 0.0) or 0.0) for c in recent]
    lows = [float(getattr(c, "low", 0.0) or 0.0) for c in recent]
    volumes = [float(getattr(c, "volume", 0.0) or 0.0) for c in recent]
    indicators = getattr(bundle, "indicators", {}) or {}

    return MarketContext(
        ticker=str(ticker or getattr(getattr(bundle, "mapping", None), "exchange_symbol", "") or ""),
        current_price=current,
        price_change_1h=_change_pct(candles, current, 3600, primary_tf),
        price_change_4h=_change_pct(candles, current, 4 * 3600, primary_tf),
        price_change_24h=_change_pct(candles, current, 24 * 3600, primary_tf),
        volume_24h=sum(volumes),
        volume_change_pct=0.0,
        high_24h=max(highs) if highs else current,
        low_24h=min(lows) if lows else current,
        bid_ask_spread=float(getattr(bundle, "bid_ask_spread_pct", 0.0) or 0.0),
        funding_rate=None,
        rsi_1h=indicators.get(primary_tf, {}).get("rsi"),
        atr_pct=indicators.get(primary_tf, {}).get("atr_pct"),
        ema_fast=indicators.get(primary_tf, {}).get("ema_fast"),
        ema_slow=indicators.get(primary_tf, {}).get("ema_slow"),
        orderbook_imbalance=None,
    )

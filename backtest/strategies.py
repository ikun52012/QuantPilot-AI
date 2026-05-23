"""
Base Strategy Classes for Backtest Engine.
Provides strategy interface and common implementations.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    EXIT = "exit"


@dataclass
class TradingSignal:
    action: str
    confidence: float = 0.0
    ticker: str = ""
    reason: str = ""
    suggested_stop_loss: float | None = None
    suggested_take_profit: float | None = None
    suggested_quantity_pct: float | None = None


class BaseStrategy(ABC):
    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.name = self.params.get("name", self.__class__.__name__)

    @abstractmethod
    def generate_signal(self, data: list[dict], current_idx: int) -> TradingSignal | None:
        pass

    def get_param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def _latest_bar(self, data: list[dict], idx: int) -> dict:
        if idx < 0 or idx >= len(data):
            return {}
        return data[idx]

    def _previous_bar(self, data: list[dict], idx: int) -> dict:
        if idx - 1 < 0 or idx - 1 >= len(data):
            return {}
        return data[idx - 1]

    def _get_price(self, data: list[dict], idx: int, price_type: str = "close") -> float:
        bar = self._latest_bar(data, idx)
        return float(bar.get(price_type, 0))

    def _get_lookback_data(self, data: list[dict], idx: int, lookback: int) -> list[dict]:
        start = max(0, idx - lookback)
        return data[start:idx + 1]


class SMCTrendStrategy(BaseStrategy):
    def __init__(self, params: dict | None = None):
        default_params = {
            "name": "smc_trend",
            "fvg_lookback": 5,
            "ob_threshold": 0.5,
            "swing_lookback": 3,
            "risk_reward_min": 1.5,
            "min_confidence": 0.6,
        }
        merged = {**default_params, **(params or {})}
        super().__init__(merged)

    def generate_signal(self, data: list[dict], current_idx: int) -> TradingSignal | None:
        if current_idx < 10:
            return None

        lookback = self.get_param("fvg_lookback", 5)
        ob_threshold = self.get_param("ob_threshold", 0.5)
        min_confidence = self.get_param("min_confidence", 0.6)

        fvg_signal = self._detect_fvg(data, current_idx, lookback)
        ob_signal = self._detect_order_block(data, current_idx, ob_threshold)
        trend = self._detect_trend(data, current_idx)

        combined_confidence = 0.0
        action = "hold"
        reason = ""

        if fvg_signal and ob_signal:
            if trend == "bullish":
                action = "buy"
                combined_confidence = min_confidence + 0.2
                reason = "Bullish FVG + OB confluence in uptrend"
            elif trend == "bearish":
                action = "sell"
                combined_confidence = min_confidence + 0.2
                reason = "Bearish FVG + OB confluence in downtrend"

        elif fvg_signal:
            if trend == "bullish":
                action = "buy"
                combined_confidence = min_confidence
                reason = "Bullish FVG in uptrend"
            elif trend == "bearish":
                action = "sell"
                combined_confidence = min_confidence
                reason = "Bearish FVG in downtrend"

        elif ob_signal:
            if trend == "bullish":
                action = "buy"
                combined_confidence = min_confidence - 0.1
                reason = "Bullish OB in uptrend"
            elif trend == "bearish":
                action = "sell"
                combined_confidence = min_confidence - 0.1
                reason = "Bearish OB in downtrend"

        if action == "hold" or combined_confidence < min_confidence:
            return None

        current_price = self._get_price(data, current_idx)
        sl_pct = self.get_param("stop_loss_pct", 2.0)
        tp_pct = sl_pct * self.get_param("risk_reward_min", 1.5)

        return TradingSignal(
            action=action,
            confidence=combined_confidence,
            ticker=self.params.get("ticker", "BTCUSDT"),
            reason=reason,
            suggested_stop_loss=current_price * (1 - sl_pct / 100) if action == "buy" else current_price * (1 + sl_pct / 100),
            suggested_take_profit=current_price * (1 + tp_pct / 100) if action == "buy" else current_price * (1 - tp_pct / 100),
        )

    def _detect_fvg(self, data: list[dict], idx: int, lookback: int) -> str | None:
        if idx < lookback + 2:
            return None

        for i in range(idx - lookback, idx - 1):
            bar1 = data[i]
            bar3 = data[i + 2] if i + 2 <= idx else None

            if not bar3:
                continue

            bar1_low = float(bar1.get("low", 0))
            bar1_high = float(bar1.get("high", 0))
            bar3_low = float(bar3.get("low", 0))
            bar3_high = float(bar3.get("high", 0))

            if bar3_low > bar1_high:
                return "bullish"

            if bar3_high < bar1_low:
                return "bearish"

        return None

    def _detect_order_block(self, data: list[dict], idx: int, threshold: float) -> str | None:
        if idx < 5:
            return None

        recent = data[idx - 5:idx + 1]

        max_impulse_up = 0.0
        max_impulse_down = 0.0

        for i in range(1, len(recent)):
            prev = recent[i - 1]
            curr = recent[i]

            prev_close = float(prev.get("close", 0))
            curr_high = float(curr.get("high", 0))
            curr_low = float(curr.get("low", 0))

            if prev_close > 0:
                impulse_up = (curr_high - prev_close) / prev_close
                impulse_down = (prev_close - curr_low) / prev_close

                if impulse_up > max_impulse_up:
                    max_impulse_up = impulse_up
                if impulse_down > max_impulse_down:
                    max_impulse_down = impulse_down

        if max_impulse_up > threshold:
            return "bullish"
        if max_impulse_down > threshold:
            return "bearish"

        return None

    def _detect_trend(self, data: list[dict], idx: int) -> str:
        if idx < 20:
            return "neutral"

        lookback_data = data[idx - 20:idx + 1]

        closes = [float(bar.get("close", 0)) for bar in lookback_data]

        if len(closes) < 2:
            return "neutral"

        first_half = closes[:len(closes) // 2]
        second_half = closes[len(closes) // 2:]

        avg_first = sum(first_half) / len(first_half) if first_half else 0
        avg_second = sum(second_half) / len(second_half) if second_half else 0

        if avg_second > avg_first * 1.02:
            return "bullish"
        if avg_second < avg_first * 0.98:
            return "bearish"

        return "neutral"


class AIAssistantStrategy(BaseStrategy):
    def __init__(self, params: dict | None = None):
        default_params = {
            "name": "ai_assistant",
            "confidence_threshold_buy": 0.75,
            "confidence_threshold_sell": 0.75,
            "risk_reward_min": 2.0,
            "max_positions": 3,
            "cooldown_bars": 10,
        }
        merged = {**default_params, **(params or {})}
        super().__init__(merged)
        self.last_signal_bar = -100

    def generate_signal(self, data: list[dict], current_idx: int) -> TradingSignal | None:
        cooldown = self.get_param("cooldown_bars", 10)
        if current_idx - self.last_signal_bar < cooldown:
            return None

        if current_idx < 20:
            return None

        ema_signal = self._ema_cross(data, current_idx)
        rsi_signal = self._rsi_signal(data, current_idx)
        volume_signal = self._volume_confirmation(data, current_idx)

        buy_threshold = self.get_param("confidence_threshold_buy", 0.75)
        sell_threshold = self.get_param("confidence_threshold_sell", 0.75)

        combined_confidence = 0.0
        action = "hold"
        reason_parts = []

        if ema_signal == "buy":
            combined_confidence += 0.3
            reason_parts.append("EMA bullish cross")
        elif ema_signal == "sell":
            combined_confidence += 0.3
            reason_parts.append("EMA bearish cross")

        if rsi_signal == "buy":
            combined_confidence += 0.25
            reason_parts.append("RSI oversold recovery")
        elif rsi_signal == "sell":
            combined_confidence += 0.25
            reason_parts.append("RSI overbought decline")

        if volume_signal:
            combined_confidence += 0.2
            reason_parts.append("Volume confirmation")

        if combined_confidence >= buy_threshold and ema_signal == "buy":
            action = "buy"
        elif combined_confidence >= sell_threshold and ema_signal == "sell":
            action = "sell"

        if action == "hold":
            return None

        self.last_signal_bar = current_idx

        current_price = self._get_price(data, current_idx)
        sl_pct = 1.5
        tp_pct = sl_pct * self.get_param("risk_reward_min", 2.0)

        return TradingSignal(
            action=action,
            confidence=combined_confidence,
            ticker=self.params.get("ticker", "BTCUSDT"),
            reason=" | ".join(reason_parts),
            suggested_stop_loss=current_price * (1 - sl_pct / 100) if action == "buy" else current_price * (1 + sl_pct / 100),
            suggested_take_profit=current_price * (1 + tp_pct / 100) if action == "buy" else current_price * (1 - tp_pct / 100),
        )

    def _ema_cross(self, data: list[dict], idx: int) -> str:
        if idx < 26:
            return "hold"

        ema_fast = self._calculate_ema(data, idx, 12)
        ema_slow = self._calculate_ema(data, idx, 26)
        prev_ema_fast = self._calculate_ema(data, idx - 1, 12)
        prev_ema_slow = self._calculate_ema(data, idx - 1, 26)

        if ema_fast <= 0 or ema_slow <= 0:
            return "hold"

        if prev_ema_fast <= prev_ema_slow and ema_fast > ema_slow:
            return "buy"
        if prev_ema_fast >= prev_ema_slow and ema_fast < ema_slow:
            return "sell"

        return "hold"

    def _calculate_ema(self, data: list[dict], idx: int, period: int) -> float:
        if idx < period:
            return 0.0

        multiplier = 2 / (period + 1)

        sma = sum(float(data[i].get("close", 0)) for i in range(idx - period, idx)) / period

        ema = sma
        for i in range(idx - period, idx + 1):
            close = float(data[i].get("close", 0))
            ema = (close - ema) * multiplier + ema

        return ema

    def _rsi_signal(self, data: list[dict], idx: int) -> str:
        if idx < 14:
            return "hold"

        rsi = self._calculate_rsi(data, idx, 14)

        if rsi < 30:
            return "buy"
        if rsi > 70:
            return "sell"

        return "hold"

    def _calculate_rsi(self, data: list[dict], idx: int, period: int) -> float:
        if idx < period + 1:
            return 50.0

        gains: list[float] = []
        losses: list[float] = []

        for i in range(idx - period, idx):
            curr_close = float(data[i + 1].get("close", 0))
            prev_close = float(data[i].get("close", 0))

            change = curr_close - prev_close
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))

        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def _volume_confirmation(self, data: list[dict], idx: int) -> bool:
        if idx < 10:
            return False

        current_volume = float(data[idx].get("volume", 0))

        avg_volume = sum(float(data[i].get("volume", 0)) for i in range(idx - 10, idx)) / 10

        if avg_volume <= 0:
            return False

        return current_volume > avg_volume * 1.2


class SimpleTrendFollowStrategy(BaseStrategy):
    def __init__(self, params: dict | None = None):
        default_params = {
            "name": "simple_trend",
            "ema_period": 20,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0,
            "min_confidence": 0.5,
        }
        merged = {**default_params, **(params or {})}
        super().__init__(merged)

    def generate_signal(self, data: list[dict], current_idx: int) -> TradingSignal | None:
        period = self.get_param("ema_period", 20)

        if current_idx < period + 2:
            return None

        current_price = self._get_price(data, current_idx)
        ema = self._calculate_simple_ema(data, current_idx, period)
        prev_ema = self._calculate_simple_ema(data, current_idx - 1, period)

        if ema <= 0 or prev_ema <= 0:
            return None

        action = "hold"
        confidence = 0.5

        if current_price > ema and data[current_idx - 1].get("close", 0) <= prev_ema:
            action = "buy"
            confidence = 0.6
            reason = "Price crossed above EMA"
        elif current_price < ema and data[current_idx - 1].get("close", 0) >= prev_ema:
            action = "sell"
            confidence = 0.6
            reason = "Price crossed below EMA"

        if action == "hold":
            return None

        sl_pct = self.get_param("stop_loss_pct", 1.5)
        tp_pct = self.get_param("take_profit_pct", 3.0)

        return TradingSignal(
            action=action,
            confidence=confidence,
            ticker=self.params.get("ticker", "BTCUSDT"),
            reason=reason,
            suggested_stop_loss=current_price * (1 - sl_pct / 100) if action == "buy" else current_price * (1 + sl_pct / 100),
            suggested_take_profit=current_price * (1 + tp_pct / 100) if action == "buy" else current_price * (1 - tp_pct / 100),
        )

    def _calculate_simple_ema(self, data: list[dict], idx: int, period: int) -> float:
        if idx < period:
            return 0.0

        multiplier = 2 / (period + 1)

        closes = [float(data[i].get("close", 0)) for i in range(idx - period + 1, idx + 1)]

        sma = sum(closes) / len(closes)

        ema = sma
        for close in closes:
            ema = (close - ema) * multiplier + ema

        return ema


class GGShotStrategy(BaseStrategy):
    """Independent GG-Shot-style range breakout strategy.

    This is a behavior-level replica based on public GG-Shot descriptions:
    range breakouts/bounces, trend-line confirmation, ATR TP/SL, volume and
    flat-market filters, and oscillator context. It does not reproduce the
    original invite-only script's private strategy table or internal tuning.
    """

    def __init__(self, params: dict | None = None):
        default_params = {
            "name": "gg_shot",
            "range_length": 20,
            "trend_mode": "balanced",
            "trend_length": 50,
            "trend_smooth": 5,
            "atr_period": 14,
            "tp1_mult": 1.5,
            "tp2_mult": 3.0,
            "tp3_mult": 4.5,
            "tp4_mult": 6.0,
            "sl_atr_mult": 1.8,
            "use_vol_filter": True,
            "vol_mult": 1.2,
            "vol_ma_length": 20,
            "use_flat_filter": True,
            "flat_atr_pct": 0.3,
            "use_oscillator": True,
            "osc_length": 14,
            "osc_low": 30,
            "osc_high": 70,
            "osc_mode": "by_trend",
            "cooldown_bars": 5,
            "min_atr_for_signal": True,
        }
        merged = {**default_params, **(params or {})}
        super().__init__(merged)
        self._signal_bar = -100
        self._entry_price: float | None = None
        self._entry_bar: int = -1
        self._direction: int = 0  # -1=short, 0=neutral, 1=long
        self._last_atr: float | None = None

    # Helpers

    def _highest(self, data: list[dict], idx: int, field: str, length: int) -> float:
        if idx < 0:
            return 0.0
        end = min(idx, len(data) - 1)
        start = max(0, end - length + 1)
        return max(float(data[i].get(field, 0)) for i in range(start, end + 1))

    def _lowest(self, data: list[dict], idx: int, field: str, length: int) -> float:
        if idx < 0:
            return 0.0
        end = min(idx, len(data) - 1)
        start = max(0, end - length + 1)
        return min(float(data[i].get(field, 0)) for i in range(start, end + 1))

    def _atr(self, data: list[dict], idx: int, period: int) -> float:
        if idx < period:
            return 0.0
        tr_values: list[float] = []
        for i in range(idx - period + 1, idx + 1):
            if i == 0:
                tr_values.append(float(data[i].get("high", 0)) - float(data[i].get("low", 0)))
            else:
                hl = float(data[i].get("high", 0)) - float(data[i].get("low", 0))
                hc = abs(float(data[i].get("high", 0)) - float(data[i - 1].get("close", 0)))
                lc = abs(float(data[i].get("low", 0)) - float(data[i - 1].get("close", 0)))
                tr_values.append(max(hl, hc, lc))
        if not tr_values:
            return 0.0
        atr_val = sum(tr_values) / len(tr_values)
        return atr_val

    def _sma(self, values: list[float], length: int) -> list[float]:
        result: list[float] = []
        for i in range(len(values)):
            if i < length - 1:
                result.append(0.0)
            else:
                result.append(sum(values[i - length + 1:i + 1]) / length)
        return result

    def _ema(self, values: list[float], period: int) -> list[float]:
        if len(values) < period:
            return values[:]
        multiplier = 2.0 / (period + 1)
        result = values[:]
        result[period - 1] = sum(values[:period]) / period
        for i in range(period, len(values)):
            result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
        return result

    def _rsi(self, data: list[dict], idx: int, period: int) -> float:
        if idx < period + 1:
            return 50.0
        gains: list[float] = []
        losses: list[float] = []
        for i in range(idx - period + 1, idx + 1):
            change = float(data[i].get("close", 0)) - float(data[i - 1].get("close", 0))
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    # Trend line

    def _trend_line(self, data: list[dict], idx: int, mode: str, high_or_low: str) -> float | None:
        if idx < 10:
            return None
        length = self.get_param("trend_length", 50)
        smooth_p = self.get_param("trend_smooth", 5)
        field = "high" if high_or_low == "high" else "low"

        if mode == "sharp":
            return self._trend_sharp(data, idx, field, 5)
        elif mode == "balanced":
            return self._trend_regression(data, idx, field, length, smooth_p)
        elif mode == "smooth":
            return self._trend_smooth(data, idx, field, length)
        return None

    def _trend_sharp(self, data: list[dict], idx: int, field: str, lookback: int) -> float | None:
        pivots: list[tuple[int, float]] = []
        for i in range(max(0, idx - lookback * 3), idx - lookback + 1):
            if i < lookback or i + lookback >= len(data):
                continue
            val = float(data[i].get(field, 0))
            is_pivot = True
            for j in range(1, lookback + 1):
                left = float(data[i - j].get(field, 0))
                right = float(data[i + j].get(field, 0))
                if field == "high" and (left >= val or right >= val):
                    is_pivot = False
                    break
                if field == "low" and (left <= val or right <= val):
                    is_pivot = False
                    break
            if is_pivot:
                pivots.append((i, val))
        if len(pivots) < 2:
            return float(data[idx].get(field, 0))
        p1, p2 = pivots[-2], pivots[-1]
        if p2[0] == p1[0]:
            return p2[1]
        slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
        return p2[1] + slope * (idx - p2[0])

    def _trend_regression(self, data: list[dict], idx: int, field: str, length: int, smooth: int) -> float:
        n = min(length, idx + 1)
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        for i in range(n):
            x = float(i)
            y = float(data[idx - n + 1 + i].get(field, 0))
            sum_x += x
            sum_y += y
            sum_xy += x * y
            sum_x2 += x * x
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return float(data[idx].get(field, 0))
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
        raw = intercept + slope * n
        if smooth <= 1 or idx < smooth:
            return raw
        vals = [self._trend_regression_raw(data, idx - smooth + 1 + i, field, length) for i in range(smooth)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else raw

    def _trend_regression_raw(self, data: list[dict], idx: int, field: str, length: int) -> float | None:
        if idx < length:
            return None
        n = length
        sum_x = 0.0
        sum_y = 0.0
        sum_xy = 0.0
        sum_x2 = 0.0
        for i in range(n):
            x = float(i)
            y = float(data[idx - n + 1 + i].get(field, 0))
            sum_x += x
            sum_y += y
            sum_xy += x * y
            sum_x2 += x * x
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return float(data[idx].get(field, 0))
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
        return intercept + slope * n

    def _trend_smooth(self, data: list[dict], idx: int, field: str, base_length: int) -> float:
        prices = [float(data[i].get(field, 0)) for i in range(max(0, idx - base_length * 2), idx + 1)]
        if not prices:
            return float(data[idx].get(field, 0))
        atr_val = self._atr(data, idx, self.get_param("atr_period", 14))
        price = float(data[idx].get("close", 0))
        if price == 0:
            return prices[-1]
        vol_ratio = atr_val / price * 100.0
        dyn_period = max(5, min(base_length * 2, int(base_length / max(vol_ratio, 0.01))))
        ema_vals = self._ema(prices, dyn_period)
        return ema_vals[-1] if ema_vals else prices[-1]

    # Signal generation

    def generate_signal(self, data: list[dict], current_idx: int) -> TradingSignal | None:
        cooldown = self.get_param("cooldown_bars", 5)
        if current_idx - self._signal_bar < cooldown:
            return None
        if current_idx < 30:
            return None

        range_len = self.get_param("range_length", 20)
        # Use the completed range before the current bar. Including the current
        # high/low makes close-based breakouts nearly impossible to trigger.
        range_high = self._highest(data, current_idx - 1, "high", range_len)
        range_low = self._lowest(data, current_idx - 1, "low", range_len)
        atr_period = self.get_param("atr_period", 14)
        atr_val = self._atr(data, current_idx, atr_period)

        if atr_val <= 0 or range_low <= 0:
            return None

        close = float(data[current_idx].get("close", 0))
        open_ = float(data[current_idx].get("open", 0))
        high = float(data[current_idx].get("high", 0))
        low = float(data[current_idx].get("low", 0))
        prev_close = float(data[current_idx - 1].get("close", 0)) if current_idx > 0 else close

        # Range boundaries
        prev_range_high = self._highest(data, current_idx - 2, "high", range_len)
        prev_range_low = self._lowest(data, current_idx - 2, "low", range_len)

        breakout_long = close > range_high and prev_close <= prev_range_high
        breakout_short = close < range_low and prev_close >= prev_range_low
        bounce_long = low <= range_low * 1.005 and close > open_ and close > range_low
        bounce_short = high >= range_high * 0.995 and close < open_ and close < range_high

        raw_long = breakout_long or bounce_long
        raw_short = breakout_short or bounce_short

        if not raw_long and not raw_short:
            return None

        # Trend line
        trend_mode = self.get_param("trend_mode", "balanced")
        trend_high = self._trend_line(data, current_idx, trend_mode, "high") or range_high
        trend_low = self._trend_line(data, current_idx, trend_mode, "low") or range_low

        prev_th = self._trend_line(data, current_idx - 1, trend_mode, "high") or range_high if current_idx > 0 else trend_high
        prev_tl = self._trend_line(data, current_idx - 1, trend_mode, "low") or range_low if current_idx > 0 else trend_low
        prev_trend_mid = (prev_th + prev_tl) / 2.0
        cur_trend_mid = (trend_high + trend_low) / 2.0
        trend_direction = 1 if cur_trend_mid > prev_trend_mid else -1 if cur_trend_mid < prev_trend_mid else 0

        # Filters
        if self.get_param("use_vol_filter", True):
            vol_ma_len = self.get_param("vol_ma_length", 20)
            vol_values = [float(data[i].get("volume", 0)) for i in range(max(0, current_idx - vol_ma_len), current_idx + 1)]
            avg_vol = sum(vol_values) / len(vol_values) if vol_values else 0
            vol_mult = self.get_param("vol_mult", 1.2)
            if avg_vol > 0 and float(data[current_idx].get("volume", 0)) < avg_vol * vol_mult:
                return None

        if self.get_param("use_flat_filter", True):
            flat_pct = self.get_param("flat_atr_pct", 0.3)
            if close > 0 and (atr_val / close * 100.0) < flat_pct:
                return None

        # Trend confirmation
        action: str = "hold"
        reason_parts: list[str] = []
        confidence = 0.0

        if raw_long and trend_direction >= 0:
            action = "buy"
            confidence = 0.65 if breakout_long else 0.55
            reason_parts.append("Long breakout" if breakout_long else "Long bounce")
            reason_parts.append("Trend rising" if trend_direction > 0 else "Trend flat")
        elif raw_short and trend_direction <= 0:
            action = "sell"
            confidence = 0.65 if breakout_short else 0.55
            reason_parts.append("Short breakout" if breakout_short else "Short bounce")
            reason_parts.append("Trend falling" if trend_direction < 0 else "Trend flat")

        if action == "hold":
            return None

        self._signal_bar = current_idx
        self._entry_price = close
        self._entry_bar = current_idx
        self._direction = 1 if action == "buy" else -1
        self._last_atr = atr_val

        # RSI oscillator check
        if self.get_param("use_oscillator", True):
            rsi = self._rsi(data, current_idx, self.get_param("osc_length", 14))
            osc_mode = self.get_param("osc_mode", "by_trend")
            osc_low = self.get_param("osc_low", 30)
            osc_high = self.get_param("osc_high", 70)
            if osc_mode == "by_trend":
                if action == "buy" and rsi < osc_low:
                    confidence += 0.1
                    reason_parts.append(f"RSI oversold ({rsi:.1f})")
                if action == "sell" and rsi > osc_high:
                    confidence += 0.1
                    reason_parts.append(f"RSI overbought ({rsi:.1f})")

        # TP / SL (ATR-based)
        sl_atr = self.get_param("sl_atr_mult", 1.8)
        tp_mult = [self.get_param("tp1_mult", 1.5), self.get_param("tp2_mult", 3.0),
                    self.get_param("tp3_mult", 4.5), self.get_param("tp4_mult", 6.0)]

        stop_loss = close - atr_val * sl_atr if action == "buy" else close + atr_val * sl_atr
        tp_levels = [(close + atr_val * m) if action == "buy" else (close - atr_val * m) for m in tp_mult]

        return TradingSignal(
            action=action,
            confidence=min(confidence, 0.95),
            ticker=self.params.get("ticker", ""),
            reason=" | ".join(reason_parts),
            suggested_stop_loss=stop_loss,
            suggested_take_profit=tp_levels[0] if tp_levels else None,
        )

    def get_tp_levels(self) -> list[float]:
        entry = self._entry_price
        if entry is None or entry <= 0:
            return []
        atr = self._last_atr if self._last_atr and self._last_atr > 0 else entry * 0.01
        tp_mult = [self.get_param("tp1_mult", 1.5), self.get_param("tp2_mult", 3.0),
                    self.get_param("tp3_mult", 4.5), self.get_param("tp4_mult", 6.0)]
        direction = self._direction
        if direction == 1:
            return [entry + atr * m for m in tp_mult]
        elif direction == -1:
            return [entry - atr * m for m in tp_mult]
        return []

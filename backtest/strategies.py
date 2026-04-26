"""
Base Strategy Classes for Backtest Engine.
Provides strategy interface and common implementations.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Any
from dataclasses import dataclass


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
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    suggested_quantity_pct: Optional[float] = None


class BaseStrategy(ABC):
    def __init__(self, params: Optional[dict] = None):
        self.params = params or {}
        self.name = self.params.get("name", self.__class__.__name__)

    @abstractmethod
    def generate_signal(self, data: list[dict], current_idx: int) -> Optional[TradingSignal]:
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
    def __init__(self, params: Optional[dict] = None):
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

    def generate_signal(self, data: list[dict], current_idx: int) -> Optional[TradingSignal]:
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

    def _detect_fvg(self, data: list[dict], idx: int, lookback: int) -> Optional[str]:
        if idx < lookback + 2:
            return None

        for i in range(idx - lookback, idx - 1):
            bar1 = data[i]
            bar2 = data[i + 1]
            bar3 = data[i + 2] if i + 2 <= idx else None

            if not bar3:
                continue

            bar1_low = float(bar1.get("low", 0))
            bar1_high = float(bar1.get("high", 0))
            bar2_high = float(bar2.get("high", 0))
            bar2_low = float(bar2.get("low", 0))
            bar3_low = float(bar3.get("low", 0))
            bar3_high = float(bar3.get("high", 0))

            if bar3_low > bar1_high:
                return "bullish"

            if bar3_high < bar1_low:
                return "bearish"

        return None

    def _detect_order_block(self, data: list[dict], idx: int, threshold: float) -> Optional[str]:
        if idx < 5:
            return None

        recent = data[idx - 5:idx + 1]

        max_impulse_up = 0
        max_impulse_down = 0

        for i in range(1, len(recent)):
            prev = recent[i - 1]
            curr = recent[i]

            prev_close = float(prev.get("close", 0))
            curr_close = float(curr.get("close", 0))
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
    def __init__(self, params: Optional[dict] = None):
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

    def generate_signal(self, data: list[dict], current_idx: int) -> Optional[TradingSignal]:
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

        gains = []
        losses = []

        for i in range(idx - period, idx):
            curr_close = float(data[i + 1].get("close", 0))
            prev_close = float(data[i].get("close", 0))

            change = curr_close - prev_close
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0

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
    def __init__(self, params: Optional[dict] = None):
        default_params = {
            "name": "simple_trend",
            "ema_period": 20,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0,
            "min_confidence": 0.5,
        }
        merged = {**default_params, **(params or {})}
        super().__init__(merged)

    def generate_signal(self, data: list[dict], current_idx: int) -> Optional[TradingSignal]:
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
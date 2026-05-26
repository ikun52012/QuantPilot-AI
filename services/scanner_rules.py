"""Scanner Scoring Rules Engine - Declarative scoring rules for scanner candidates.

This module provides a flexible, declarative scoring system that:
1. Separates scoring logic from scoring configuration
2. Supports runtime rule editing via Admin UI
3. Enables backtesting different rule configurations
4. Provides factor correlation analysis and orthogonalization
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import settings


@dataclass
class ScoringContext:
    """Context object passed to scoring rules containing all market data."""
    direction: str
    current_price: float
    atr_pct: float
    atr_price: float
    rsi: float
    ema_fast: float
    ema_slow: float
    ema200: float
    macd_hist: float
    adx: float
    volume_ratio: float
    vwap: float
    vwap_distance_pct: float
    poc: float
    regime: str
    oi_change_pct: float | None
    htf_trend: str | None
    smc_trend: str
    smc_risk_score: float
    smc_timing_score: float
    price_zone: str
    support_zone: dict[str, Any] | None
    premium_zone: float
    discount_zone: float
    equilibrium: float
    spread_pct: float
    bid_ask_spread_pct: float
    bundle_quality_passed: bool
    bundle_quality_reasons: list[str]
    timeframe: str
    market_type: str

    @property
    def expected_trend(self) -> str:
        """Return expected SMC trend for current direction."""
        return "bullish" if self.direction == "long" else "bearish"

    @property
    def is_trending(self) -> bool:
        return self.regime == "trending"

    @property
    def is_ranging(self) -> bool:
        return self.regime == "ranging"

    @property
    def has_support(self) -> bool:
        return self.support_zone is not None

    @property
    def in_discount(self) -> bool:
        return self.discount_zone > 0 and self.current_price <= self.discount_zone

    @property
    def in_premium(self) -> bool:
        return self.premium_zone > 0 and self.current_price >= self.premium_zone

    @property
    def near_equilibrium(self) -> bool:
        return self.equilibrium > 0 and self.current_price <= self.equilibrium

    @property
    def ema_aligned_bullish(self) -> bool:
        return self.ema_fast > 0 and self.ema_slow > 0 and self.ema_fast > self.ema_slow

    @property
    def ema_aligned_bearish(self) -> bool:
        return self.ema_fast > 0 and self.ema_slow > 0 and self.ema_fast < self.ema_slow

    @property
    def rsi_oversold(self) -> bool:
        return self.rsi <= float(settings.scanner.rsi_lower)

    @property
    def rsi_overbought(self) -> bool:
        return self.rsi >= float(settings.scanner.rsi_upper)

    @property
    def atr_acceptable(self) -> bool:
        return self.atr_pct >= float(settings.scanner.min_atr_pct)

    @property
    def spread_acceptable(self) -> bool:
        return self.spread_pct <= float(settings.scanner.max_spread_pct)

    @property
    def volume_acceptable(self) -> bool:
        return self.volume_ratio >= 1.0

    @property
    def macd_confirms_direction(self) -> bool:
        if self.direction == "long":
            return self.macd_hist > 0
        return self.macd_hist < 0

    @property
    def macd_conflicts_direction(self) -> bool:
        if self.direction == "long":
            return self.macd_hist < 0
        return self.macd_hist > 0

    @property
    def adx_strong(self) -> bool:
        return self.adx >= 18

    @property
    def above_vwap(self) -> bool:
        return self.vwap > 0 and self.current_price > self.vwap

    @property
    def below_vwap(self) -> bool:
        return self.vwap > 0 and self.current_price < self.vwap

    @property
    def above_poc(self) -> bool:
        return self.poc > 0 and self.current_price > self.poc

    @property
    def below_poc(self) -> bool:
        return self.poc > 0 and self.current_price < self.poc

    @property
    def oi_rising(self) -> bool:
        return self.oi_change_pct is not None and self.oi_change_pct > 3.0

    @property
    def oi_falling(self) -> bool:
        return self.oi_change_pct is not None and self.oi_change_pct < -3.0

    @property
    def above_ema200(self) -> bool:
        return self.ema200 > 0 and self.current_price > self.ema200

    @property
    def below_ema200(self) -> bool:
        return self.ema200 > 0 and self.current_price < self.ema200

    @property
    def htf_bullish(self) -> bool:
        return self.htf_trend == "bullish"

    @property
    def htf_bearish(self) -> bool:
        return self.htf_trend == "bearish"

    @property
    def htf_conflicts(self) -> bool:
        if self.direction == "long":
            return self.htf_bearish
        return self.htf_bullish


@dataclass
class ScoringRule:
    """A single scoring rule with condition, base score, and weight key."""
    name: str
    base_score: float
    weight_key: str = "default"
    condition: str = ""
    category: str = "indicator"
    description: str = ""
    regime_modifier: dict[str, float] = field(default_factory=dict)
    enabled: bool = True
    penalty: bool = False

    def evaluate(self, ctx: ScoringContext, weights: dict[str, float], regime: str) -> float:
        """Evaluate rule and return score contribution."""
        if not self.enabled:
            return 0.0

        condition_fn = RULE_CONDITIONS.get(self.condition)
        if condition_fn is None:
            return 0.0

        try:
            matches = condition_fn(ctx)
        except Exception:
            return 0.0

        if not matches:
            return 0.0

        base = -abs(self.base_score) if self.penalty else self.base_score

        regime_mod = self.regime_modifier.get(regime, 0.0)
        base += regime_mod

        weight = weights.get(self.weight_key, 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = 1.0

        return base * weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_score": self.base_score,
            "weight_key": self.weight_key,
            "condition": self.condition,
            "category": self.category,
            "description": self.description,
            "regime_modifier": self.regime_modifier,
            "enabled": self.enabled,
            "penalty": self.penalty,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoringRule:
        return cls(
            name=str(data.get("name", "")),
            base_score=float(data.get("base_score", 0.0)),
            weight_key=str(data.get("weight_key", "default")),
            condition=str(data.get("condition", "")),
            category=str(data.get("category", "indicator")),
            description=str(data.get("description", "")),
            regime_modifier=dict(data.get("regime_modifier", {})),
            enabled=bool(data.get("enabled", True)),
            penalty=bool(data.get("penalty", False)),
        )


RULE_CONDITIONS: dict[str, Callable[[ScoringContext], bool]] = {}


def register_condition(name: str, fn: Callable[[ScoringContext], bool]) -> None:
    """Register a condition function for use in rules."""
    RULE_CONDITIONS[name] = fn


def _init_default_conditions() -> None:
    """Initialize default condition functions."""
    register_condition("ema_aligned_long", lambda ctx: ctx.direction == "long" and ctx.ema_aligned_bullish and ctx.current_price >= ctx.ema_fast)
    register_condition("ema_aligned_short", lambda ctx: ctx.direction == "short" and ctx.ema_aligned_bearish and ctx.current_price <= ctx.ema_fast)
    register_condition("rsi_oversold", lambda ctx: ctx.direction == "long" and ctx.rsi_oversold)
    register_condition("rsi_overbought", lambda ctx: ctx.direction == "short" and ctx.rsi_overbought)
    register_condition("smc_trend_bullish", lambda ctx: ctx.direction == "long" and ctx.smc_trend == "bullish")
    register_condition("smc_trend_bearish", lambda ctx: ctx.direction == "short" and ctx.smc_trend == "bearish")
    register_condition("smc_ranging", lambda ctx: ctx.smc_trend == "ranging")
    register_condition("in_discount", lambda ctx: ctx.direction == "long" and ctx.in_discount)
    register_condition("in_premium", lambda ctx: ctx.direction == "short" and ctx.in_premium)
    register_condition("below_equilibrium", lambda ctx: ctx.direction == "long" and ctx.near_equilibrium)
    register_condition("above_equilibrium", lambda ctx: ctx.direction == "short" and ctx.equilibrium > 0 and ctx.current_price >= ctx.equilibrium)
    register_condition("atr_acceptable", lambda ctx: ctx.atr_acceptable)
    register_condition("spread_acceptable", lambda ctx: ctx.spread_acceptable)
    register_condition("macd_confirms", lambda ctx: ctx.macd_confirms_direction)
    register_condition("macd_conflicts", lambda ctx: ctx.macd_conflicts_direction)
    register_condition("adx_strong", lambda ctx: ctx.adx_strong)
    register_condition("volume_acceptable", lambda ctx: ctx.volume_acceptable)
    register_condition("volume_low", lambda ctx: ctx.volume_ratio < float(settings.scanner.min_volume_ratio))
    register_condition("has_support", lambda ctx: ctx.has_support)
    register_condition("no_support", lambda ctx: not ctx.has_support)
    register_condition("risk_low", lambda ctx: ctx.smc_risk_score < 0.5)
    register_condition("timing_good", lambda ctx: ctx.smc_timing_score > 0.5)
    register_condition("above_ema200_long", lambda ctx: ctx.direction == "long" and ctx.above_ema200)
    register_condition("below_ema200_short", lambda ctx: ctx.direction == "short" and ctx.below_ema200)
    register_condition("ema200_conflict_long", lambda ctx: ctx.direction == "long" and ctx.ema200 > 0 and not ctx.above_ema200)
    register_condition("ema200_conflict_short", lambda ctx: ctx.direction == "short" and ctx.ema200 > 0 and not ctx.below_ema200)
    register_condition("htf_conflicts", lambda ctx: ctx.htf_trend and ctx.htf_conflicts)
    register_condition("above_vwap_long", lambda ctx: ctx.direction == "long" and ctx.above_vwap)
    register_condition("below_vwap_short", lambda ctx: ctx.direction == "short" and ctx.below_vwap)
    register_condition("vwap_conflicts", lambda ctx: ctx.vwap > 0 and ctx.vwap_distance_pct is not None and ((ctx.direction == "long" and ctx.below_vwap) or (ctx.direction == "short" and ctx.above_vwap)))
    register_condition("above_poc_long", lambda ctx: ctx.direction == "long" and ctx.above_poc)
    register_condition("below_poc_short", lambda ctx: ctx.direction == "short" and ctx.below_poc)
    register_condition("oi_rising_long", lambda ctx: ctx.direction == "long" and ctx.oi_rising)
    register_condition("oi_falling_short", lambda ctx: ctx.direction == "short" and ctx.oi_falling)
    register_condition("oi_divergence_long", lambda ctx: ctx.direction == "long" and ctx.oi_falling)
    register_condition("oi_divergence_short", lambda ctx: ctx.direction == "short" and ctx.oi_rising)
    register_condition("regime_trending", lambda ctx: ctx.is_trending)
    register_condition("regime_ranging", lambda ctx: ctx.is_ranging)


_init_default_conditions()


DEFAULT_RULES: list[ScoringRule] = [
    ScoringRule(
        name="ema_alignment",
        base_score=16.0,
        weight_key="ema_alignment",
        condition="ema_aligned_long",
        category="trend",
        description="EMA bullish alignment with price above fast EMA",
        regime_modifier={"trending": 4.0, "ranging": -2.0},
    ),
    ScoringRule(
        name="ema_alignment_short",
        base_score=16.0,
        weight_key="ema_alignment",
        condition="ema_aligned_short",
        category="trend",
        description="EMA bearish alignment with price below fast EMA",
        regime_modifier={"trending": 4.0, "ranging": -2.0},
    ),
    ScoringRule(
        name="rsi_extreme",
        base_score=12.0,
        weight_key="rsi_extreme",
        condition="rsi_oversold",
        category="oscillator",
        description="RSI oversold for long entry",
        regime_modifier={"ranging": 6.0},
    ),
    ScoringRule(
        name="rsi_extreme_short",
        base_score=12.0,
        weight_key="rsi_extreme",
        condition="rsi_overbought",
        category="oscillator",
        description="RSI overbought for short entry",
        regime_modifier={"ranging": 6.0},
    ),
    ScoringRule(
        name="smc_trend",
        base_score=18.0,
        weight_key="smc_trend",
        condition="smc_trend_bullish",
        category="structure",
        description="SMC trend bullish",
        regime_modifier={"trending": 6.0},
    ),
    ScoringRule(
        name="smc_trend_short",
        base_score=18.0,
        weight_key="smc_trend",
        condition="smc_trend_bearish",
        category="structure",
        description="SMC trend bearish",
        regime_modifier={"trending": 6.0},
    ),
    ScoringRule(
        name="smc_ranging",
        base_score=8.0,
        weight_key="smc_ranging",
        condition="smc_ranging",
        category="structure",
        description="SMC ranging market",
        regime_modifier={"ranging": 4.0, "trending": -4.0},
    ),
    ScoringRule(
        name="price_zone_discount",
        base_score=22.0,
        weight_key="price_zone",
        condition="in_discount",
        category="zone",
        description="Price in discount zone for long",
    ),
    ScoringRule(
        name="price_zone_premium",
        base_score=22.0,
        weight_key="price_zone",
        condition="in_premium",
        category="zone",
        description="Price in premium zone for short",
    ),
    ScoringRule(
        name="below_equilibrium",
        base_score=12.0,
        weight_key="price_zone",
        condition="below_equilibrium",
        category="zone",
        description="Price below equilibrium for long",
    ),
    ScoringRule(
        name="above_equilibrium",
        base_score=12.0,
        weight_key="price_zone",
        condition="above_equilibrium",
        category="zone",
        description="Price above equilibrium for short",
    ),
    ScoringRule(
        name="atr",
        base_score=12.0,
        weight_key="atr",
        condition="atr_acceptable",
        category="volatility",
        description="ATR volatility acceptable",
        regime_modifier={"trending": 2.0},
    ),
    ScoringRule(
        name="spread",
        base_score=6.0,
        weight_key="spread",
        condition="spread_acceptable",
        category="execution",
        description="Spread within limits",
    ),
    ScoringRule(
        name="macd_confirmation",
        base_score=6.0,
        weight_key="macd_confirmation",
        condition="macd_confirms",
        category="momentum",
        description="MACD confirms direction",
        regime_modifier={"trending": 2.0},
    ),
    ScoringRule(
        name="macd_penalty",
        base_score=-4.0,
        weight_key="macd_confirmation",
        condition="macd_conflicts",
        category="momentum",
        description="MACD conflicts direction penalty",
        penalty=True,
        regime_modifier={"trending": -2.0},
    ),
    ScoringRule(
        name="adx_confirmation",
        base_score=4.0,
        weight_key="adx_confirmation",
        condition="adx_strong",
        category="trend_strength",
        description="ADX trend strength acceptable",
        regime_modifier={"trending": 3.0},
    ),
    ScoringRule(
        name="volume_confirmation",
        base_score=4.0,
        weight_key="volume_confirmation",
        condition="volume_acceptable",
        category="volume",
        description="Volume confirms participation",
    ),
    ScoringRule(
        name="volume_penalty",
        base_score=-6.0,
        weight_key="volume_confirmation",
        condition="volume_low",
        category="volume",
        description="Low volume penalty",
        penalty=True,
    ),
    ScoringRule(
        name="support_zone",
        base_score=24.0,
        weight_key="support_zone",
        condition="has_support",
        category="structure",
        description="Near support zone (FVG/OB)",
    ),
    ScoringRule(
        name="no_support_penalty",
        base_score=-10.0,
        weight_key="support_zone",
        condition="no_support",
        category="structure",
        description="No nearby support penalty",
        penalty=True,
    ),
    ScoringRule(
        name="risk_score",
        base_score=8.0,
        weight_key="risk",
        condition="risk_low",
        category="risk",
        description="Low SMC risk score bonus",
    ),
    ScoringRule(
        name="timing_score",
        base_score=8.0,
        weight_key="timing",
        condition="timing_good",
        category="timing",
        description="Good entry timing score bonus",
    ),
    ScoringRule(
        name="ema200_alignment_long",
        base_score=10.0,
        weight_key="ema200_alignment",
        condition="above_ema200_long",
        category="macro_trend",
        description="EMA200 bullish alignment",
        regime_modifier={"trending": 5.0},
    ),
    ScoringRule(
        name="ema200_alignment_short",
        base_score=10.0,
        weight_key="ema200_alignment",
        condition="below_ema200_short",
        category="macro_trend",
        description="EMA200 bearish alignment",
        regime_modifier={"trending": 5.0},
    ),
    ScoringRule(
        name="ema200_conflict_long",
        base_score=-15.0,
        weight_key="ema200_conflict",
        condition="ema200_conflict_long",
        category="macro_trend",
        description="EMA200 conflict penalized for long",
        penalty=True,
        regime_modifier={"trending": -8.0},
    ),
    ScoringRule(
        name="ema200_conflict_short",
        base_score=-15.0,
        weight_key="ema200_conflict",
        condition="ema200_conflict_short",
        category="macro_trend",
        description="EMA200 conflict penalized for short",
        penalty=True,
        regime_modifier={"trending": -8.0},
    ),
    ScoringRule(
        name="htf_conflict",
        base_score=-20.0,
        weight_key="htf_conflict",
        condition="htf_conflicts",
        category="structure",
        description="HTF structure conflict penalty",
        penalty=True,
    ),
    ScoringRule(
        name="vwap_long",
        base_score=6.0,
        weight_key="vwap",
        condition="above_vwap_long",
        category="volume",
        description="Price above VWAP for long",
    ),
    ScoringRule(
        name="vwap_short",
        base_score=6.0,
        weight_key="vwap",
        condition="below_vwap_short",
        category="volume",
        description="Price below VWAP for short",
    ),
    ScoringRule(
        name="vwap_penalty",
        base_score=-4.0,
        weight_key="vwap",
        condition="vwap_conflicts",
        category="volume",
        description="VWAP conflict penalty",
        penalty=True,
    ),
    ScoringRule(
        name="poc_long",
        base_score=5.0,
        weight_key="poc",
        condition="above_poc_long",
        category="volume_profile",
        description="Price favorable to POC for long",
    ),
    ScoringRule(
        name="poc_short",
        base_score=5.0,
        weight_key="poc",
        condition="below_poc_short",
        category="volume_profile",
        description="Price favorable to POC for short",
    ),
    ScoringRule(
        name="oi_confirmation_long",
        base_score=6.0,
        weight_key="oi_confirmation",
        condition="oi_rising_long",
        category="open_interest",
        description="OI rising confirms long",
    ),
    ScoringRule(
        name="oi_confirmation_short",
        base_score=6.0,
        weight_key="oi_confirmation",
        condition="oi_falling_short",
        category="open_interest",
        description="OI falling confirms short",
    ),
    ScoringRule(
        name="oi_divergence_long",
        base_score=-5.0,
        weight_key="oi_divergence",
        condition="oi_divergence_long",
        category="open_interest",
        description="OI divergence penalty for long",
        penalty=True,
    ),
    ScoringRule(
        name="oi_divergence_short",
        base_score=-5.0,
        weight_key="oi_divergence",
        condition="oi_divergence_short",
        category="open_interest",
        description="OI divergence penalty for short",
        penalty=True,
    ),
    ScoringRule(
        name="regime_trending_bonus",
        base_score=4.0,
        weight_key="regime_trending",
        condition="regime_trending",
        category="regime",
        description="Trending market regime bonus",
    ),
    ScoringRule(
        name="regime_ranging_penalty",
        base_score=-6.0,
        weight_key="regime_ranging",
        condition="regime_ranging",
        category="regime",
        description="ranging market regime penalized",
        penalty=True,
    ),
]


class ScoringEngine:
    """Declarative scoring engine for scanner candidates."""

    def __init__(self, rules: list[ScoringRule] | None = None, weights: dict[str, float] | None = None):
        self.rules = rules or DEFAULT_RULES.copy()
        self.weights = weights or dict(settings.scanner.score_weights or {})
        self._correlation_cache: dict[str, float] = {}
        self._orthogonal_groups: list[list[str]] = []

    def evaluate(self, ctx: ScoringContext) -> tuple[float, list[str], dict[str, float]]:
        """Evaluate all rules and return total score, reasons, and breakdown."""
        total = 0.0
        reasons: list[str] = []
        breakdown: dict[str, float] = {}
        regime = ctx.regime

        for rule in self.rules:
            if not self._runtime_rule_enabled(rule):
                continue
            contribution = rule.evaluate(ctx, self.weights, regime)
            if abs(contribution) >= 0.5:
                breakdown[rule.name] = contribution
                total += contribution
                if contribution > 0:
                    reasons.append(rule.description)
                elif contribution < 0 and rule.penalty:
                    reasons.append(f"{rule.description} ({contribution:.0f})")

        total = max(0.0, min(100.0, total))
        return round(total, 2), reasons, breakdown

    @staticmethod
    def _runtime_rule_enabled(rule: ScoringRule) -> bool:
        """Honor scanner feature toggles even when rules are customized at runtime."""
        if rule.weight_key in {"ema200_alignment", "ema200_conflict"} or rule.name.startswith("ema200_"):
            return bool(settings.scanner.ema200_enabled)
        if rule.weight_key == "htf_conflict" or rule.condition == "htf_conflicts":
            return bool(settings.scanner.htf_conflict_enabled)
        if rule.category == "regime" or rule.weight_key in {"regime_trending", "regime_ranging"}:
            return bool(settings.scanner.regime_filter_enabled)
        return True

    def set_weights(self, weights: dict[str, float]) -> None:
        """Update scoring weights."""
        self.weights = weights

    def set_rules(self, rules: list[ScoringRule]) -> None:
        """Update scoring rules."""
        self.rules = rules

    def get_rules(self) -> list[dict[str, Any]]:
        """Return rules as list of dicts for API/UI."""
        return [r.to_dict() for r in self.rules]

    def analyze_correlations(self, history: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        """Analyze rule correlations from historical scoring data.

        Returns correlation matrix between rules to identify redundant factors.
        """
        if len(history) < 10:
            return {}

        rule_values: dict[str, list[float]] = {}
        for record in history:
            breakdown = record.get("score_breakdown", {})
            for rule_name, value in breakdown.items():
                rule_values.setdefault(rule_name, []).append(float(value))

        correlations: dict[str, dict[str, float]] = {}
        rule_names = list(rule_values.keys())

        for i, name_a in enumerate(rule_names):
            correlations[name_a] = {}
            values_a = rule_values[name_a]
            mean_a = sum(values_a) / len(values_a)
            var_a = sum((v - mean_a) ** 2 for v in values_a)

            for name_b in rule_names[i + 1:]:
                values_b = rule_values[name_b]
                if len(values_a) != len(values_b):
                    continue
                mean_b = sum(values_b) / len(values_b)
                var_b = sum((v - mean_b) ** 2 for v in values_b)

                if var_a == 0 or var_b == 0:
                    correlations[name_a][name_b] = 0.0
                    continue

                cov = sum((values_a[k] - mean_a) * (values_b[k] - mean_b) for k in range(len(values_a)))
                corr = cov / (math.sqrt(var_a) * math.sqrt(var_b))
                correlations[name_a][name_b] = round(corr, 3)

        return correlations

    def identify_orthogonal_groups(self, correlations: dict[str, dict[str, float]], threshold: float = 0.7) -> list[list[str]]:
        """Identify groups of highly correlated rules for orthogonalization.

        Rules with correlation > threshold are grouped together.
        Only one rule from each group should be weighted heavily.
        """
        groups: list[list[str]] = []
        assigned: set[str] = set()
        for rule_a, corr_dict in correlations.items():
            if rule_a in assigned:
                continue
            group = [rule_a]
            assigned.add(rule_a)
            for rule_b, corr in corr_dict.items():
                if rule_b in assigned:
                    continue
                if abs(corr) >= threshold:
                    group.append(rule_b)
                    assigned.add(rule_b)
            if len(group) > 1:
                groups.append(group)

        return groups

    def suggest_weight_adjustments(self, correlations: dict[str, dict[str, float]]) -> dict[str, float]:
        """Suggest weight adjustments to reduce factor redundancy.

        For highly correlated groups, reduce weights of secondary factors.
        """
        groups = self.identify_orthogonal_groups(correlations)
        suggestions: dict[str, float] = {}

        for group in groups:
            sorted_group = sorted(group, key=lambda n: abs(self.weights.get(n, 1.0)))
            for i, rule_name in enumerate(sorted_group):
                if i == 0:
                    suggestions[rule_name] = 1.0
                else:
                    suggestions[rule_name] = 0.5

        return suggestions

    def to_json(self) -> str:
        """Serialize engine config to JSON."""
        return json.dumps({
            "rules": self.get_rules(),
            "weights": self.weights,
            "orthogonal_groups": self._orthogonal_groups,
        }, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> ScoringEngine:
        """Deserialize engine config from JSON."""
        data = json.loads(json_str)
        rules = [ScoringRule.from_dict(r) for r in data.get("rules", [])]
        weights = dict(data.get("weights", {}))
        return cls(rules=rules, weights=weights)


DEFAULT_ENGINE = ScoringEngine()


def get_scoring_engine() -> ScoringEngine:
    """Get the global scoring engine instance."""
    return DEFAULT_ENGINE


def save_rules_config(path: str | Path | None = None) -> None:
    """Save current rules configuration to file."""
    if path is None:
        path = Path(settings.data_dir) / "scanner_rules.json"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_ENGINE.to_json(), encoding="utf-8")
    logger.info(f"[ScannerRules] Saved rules config to {path}")


def load_rules_config(path: str | Path | None = None) -> bool:
    """Load rules configuration from file."""
    if path is None:
        path = Path(settings.data_dir) / "scanner_rules.json"
    path = Path(path)
    if not path.exists():
        return False
    try:
        data = path.read_text(encoding="utf-8")
        engine = ScoringEngine.from_json(data)
        DEFAULT_ENGINE.rules = engine.rules
        DEFAULT_ENGINE.weights = engine.weights
        logger.info(f"[ScannerRules] Loaded rules config from {path}")
        return True
    except Exception as e:
        logger.warning(f"[ScannerRules] Failed to load rules config: {e}")
        return False

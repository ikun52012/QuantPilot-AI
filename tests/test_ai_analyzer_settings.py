import json
from types import SimpleNamespace

import pytest

from ai_analyzer import (
    _AI_CACHE,
    _SMC_CACHE,
    _analysis_config_signature,
    _build_user_prompt,
    _cached_analyze_smc_single_tf,
    _get_cached_analysis,
    _get_effective_system_prompt,
    _parse_response,
    _set_cached_analysis,
    validate_ai_analysis_against_signal,
)
from core.config import settings
from core.database import PositionModel
from models import AIAnalysis, MarketContext, TradingViewSignal
from position_monitor import _paper_trailing_stop_price
from services.signal_processor import SignalProcessor


@pytest.fixture(autouse=True)
def clear_ai_cache():
    _AI_CACHE.clear()
    yield
    _AI_CACHE.clear()


@pytest.fixture
def sample_signal():
    return TradingViewSignal(
        secret="test",
        ticker="BTCUSDT",
        exchange="BINANCE",
        direction="long",
        price=50000.0,
        timeframe="60",
        strategy="test",
        message="",
    )


@pytest.fixture
def sample_market():
    return MarketContext(
        ticker="BTCUSDT",
        current_price=50000.0,
        price_change_1h=1.0,
        volume_24h=1000000.0,
    )


def test_effective_system_prompt_uses_user_risk_and_tp_settings(monkeypatch):
    monkeypatch.setattr(settings.risk, "ai_risk_profile", "balanced")
    monkeypatch.setattr(settings.risk, "exit_management_mode", "ai")
    monkeypatch.setattr(settings.risk, "ai_exit_system_prompt", "")
    monkeypatch.setattr(settings.take_profit, "num_levels", 1)

    prompt = _get_effective_system_prompt(
        {
            "risk": {
                "ai_risk_profile": "aggressive",
                "exit_management_mode": "ai",
                "ai_exit_system_prompt": "Use wider stops.",
            },
            "take_profit": {"num_levels": 3},
        }
    )

    assert "AI risk profile: AGGRESSIVE." in prompt
    assert "exactly 3 take-profit targets" in prompt
    assert "Use wider stops." in prompt


def test_build_user_prompt_respects_custom_exit_mode(sample_signal, sample_market, monkeypatch):
    monkeypatch.setattr(settings.risk, "exit_management_mode", "ai")
    prompt = _build_user_prompt(
        sample_signal,
        sample_market,
        user_settings={
            "risk": {"exit_management_mode": "custom"},
            "take_profit": {"num_levels": 4},
        },
    )

    assert "The server is using custom fixed exits" in prompt
    assert "Generate valid prices for suggested_tp1" not in prompt


def test_build_user_prompt_includes_modify_and_null_field_contract(sample_signal, sample_market, monkeypatch):
    monkeypatch.setattr(settings.risk, "exit_management_mode", "ai")

    prompt = _build_user_prompt(
        sample_signal,
        sample_market,
        user_settings={"take_profit": {"num_levels": 2}},
    )

    assert "Use recommendation='modify' only if you also provide a valid suggested_entry" in prompt
    assert "suggested_stop_loss must be a finite numeric price" in prompt
    assert "For LONG: stop loss must be below final entry" in prompt
    assert "If no valid stop loss can be calculated" in prompt
    assert "If recommendation is 'reject' or 'hold', set suggested_entry, suggested_stop_loss, and all TP fields to null" in prompt


def test_build_user_prompt_includes_prefilter_context(sample_signal, sample_market, monkeypatch):
    monkeypatch.setattr(settings.risk, "exit_management_mode", "ai")

    prompt = _build_user_prompt(
        sample_signal,
        sample_market,
        user_settings={
            "_prefilter_summary": {
                "score": 72.5,
                "hard_fail_count": 0,
                "soft_fail_count": 2,
                "notable_checks": ["spread", "funding_rate"],
            }
        },
    )

    assert "## Pre-Filter Context" in prompt
    assert "Pre-filter Score: 72.5" in prompt
    assert "Soft Fails Before AI: 2" in prompt
    assert "Notable Checks: spread; funding_rate" in prompt


def test_analysis_config_signature_changes_with_prefilter_summary():
    baseline = _analysis_config_signature({"_prefilter_summary": {"score": 80.0, "soft_fail_count": 1}})
    changed = _analysis_config_signature({"_prefilter_summary": {"score": 65.0, "soft_fail_count": 2}})
    assert baseline != changed


@pytest.mark.asyncio
async def test_ai_cache_isolated_by_effective_settings():
    analysis = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="ok")
    sig_one = _analysis_config_signature({"take_profit": {"num_levels": 1}})
    sig_two = _analysis_config_signature({"take_profit": {"num_levels": 3}})

    await _set_cached_analysis("BTCUSDT", "long", analysis, "50000.00", "60", sig_one)

    assert await _get_cached_analysis("BTCUSDT", "long", "50000.00", "60", sig_one) is analysis
    assert await _get_cached_analysis("BTCUSDT", "long", "50000.00", "60", sig_two) is None


def test_analysis_config_signature_changes_with_zero_preserved_trailing_value():
    baseline = _analysis_config_signature({"trailing_stop": {"activation_profit_pct": 0.0}})
    changed = _analysis_config_signature({"trailing_stop": {"activation_profit_pct": 1.0}})
    assert baseline != changed


def test_custom_exit_plan_preserves_zero_qty_override(monkeypatch):
    monkeypatch.setattr(settings.take_profit, "num_levels", 2)
    monkeypatch.setattr(settings.take_profit, "tp1_pct", 2.0)
    monkeypatch.setattr(settings.take_profit, "tp2_pct", 4.0)
    monkeypatch.setattr(settings.take_profit, "tp1_qty", 50.0)
    monkeypatch.setattr(settings.take_profit, "tp2_qty", 50.0)
    monkeypatch.setattr(settings.risk, "custom_stop_loss_pct", 1.5)

    processor = SignalProcessor(session=None)
    signal = TradingViewSignal(
        secret="test",
        ticker="BTCUSDT",
        exchange="BINANCE",
        direction="long",
        price=100.0,
        timeframe="60",
        strategy="test",
        message="",
    )
    analysis = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="ok")
    decision = processor._build_trade_decision(
        signal,
        analysis,
        MarketContext(ticker="BTCUSDT", current_price=100.0),
        None,
        {
            "risk": {"exit_management_mode": "custom"},
            "take_profit": {
                "num_levels": 2,
                "tp1_pct": 3.0,  # 3% above entry → R:R = 3%/1.5% = 2:1
                "tp2_pct": 5.0,
                "tp1_qty": 100.0,
                "tp2_qty": 0.0,
            },
        },
    )

    assert decision.execute is True
    assert len(decision.take_profit_levels) == 1
    assert decision.take_profit_levels[0].qty_pct == 100.0
    assert decision.take_profit_levels[0].price == 103.0  # entry * (1 + 3%)


def test_paper_trailing_stop_price_preserves_zero_activation(monkeypatch):
    monkeypatch.setattr(settings.trailing_stop, "activation_profit_pct", 1.0)
    monkeypatch.setattr(settings.trailing_stop, "trail_pct", 1.0)

    position = PositionModel(
        direction="long",
        entry_price=100.0,
        trailing_stop_config_json=json.dumps({"mode": "moving", "trail_pct": 1.0, "activation_profit_pct": 0.0}),
    )

    new_stop = _paper_trailing_stop_price(position, 100.0)

    assert new_stop == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_smc_cache_isolated_by_direction(monkeypatch):
    _SMC_CACHE.clear()

    def fake_analyze_smc_single_tf(ohlcv, timeframe, current_price, signal_direction, atr_pct):
        return SimpleNamespace(
            timeframe=timeframe,
            fvgs=[],
            order_blocks=[],
            structure=None,
            premium_zone=0.0,
            discount_zone=0.0,
            equilibrium=0.0,
            risk_score=0.5,
            entry_timing_score=0.5,
            timing_recommendation=signal_direction,
        )

    ohlcv = [[i, 100 + i, 102 + i, 99 + i, 101 + i, 1000] for i in range(8)]
    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze_smc_single_tf)

    long_ctx = await _cached_analyze_smc_single_tf("BTCUSDT", ohlcv, "1h", 108.0, "long", 0.5)
    short_ctx = await _cached_analyze_smc_single_tf("BTCUSDT", ohlcv, "1h", 108.0, "short", 0.5)

    assert long_ctx.timing_recommendation == "long"
    assert short_ctx.timing_recommendation == "short"
    assert len(_SMC_CACHE) == 2

    _SMC_CACHE.clear()


def test_parse_response_clamps_position_size_to_model_limit():
    result = _parse_response(json.dumps({"confidence": 0.8, "recommendation": "hold", "position_size_pct": 10}))

    assert result.position_size_pct == 1.0


def test_parse_response_rejects_low_confidence_modify():
    result = _parse_response(json.dumps({
        "confidence": 0.3,
        "recommendation": "modify",
        "suggested_stop_loss": 95.0,
        "reasoning": "maybe",
    }))

    assert result.recommendation == "reject"
    assert "below execute threshold" in result.reasoning


def test_parse_response_caps_tp_sum_after_negative_sanitization():
    result = _parse_response(json.dumps({
        "confidence": 0.8,
        "recommendation": "hold",
        "tp1_qty_pct": 100,
        "tp2_qty_pct": 100,
        "tp3_qty_pct": -100,
        "tp4_qty_pct": 0,
    }))

    assert result.tp1_qty_pct + result.tp2_qty_pct + result.tp3_qty_pct + result.tp4_qty_pct == pytest.approx(100.0)


def test_parse_response_rejects_nonfinite_stop_loss():
    result = _parse_response(
        '{"confidence":0.8,"recommendation":"execute","suggested_stop_loss":NaN,"reasoning":"bad sl"}'
    )

    assert result.recommendation == "reject"
    assert result.suggested_stop_loss is None


def test_parse_response_allows_missing_stop_loss_for_processor_fallback():
    result = _parse_response(json.dumps({
        "confidence": 0.8,
        "recommendation": "execute",
        "reasoning": "ok",
    }))

    assert result.recommendation == "execute"
    assert result.suggested_stop_loss is None
    assert any("server will apply fallback" in warning for warning in result.warnings)


def test_validate_ai_analysis_clears_wrong_side_stop_loss(sample_signal, sample_market):
    analysis = AIAnalysis(
        confidence=0.8,
        recommendation="execute",
        reasoning="ok",
        suggested_stop_loss=51000.0,
        suggested_tp1=53000.0,
    )

    result = validate_ai_analysis_against_signal(sample_signal, sample_market, analysis)

    assert result.recommendation == "execute"
    assert result.suggested_stop_loss is None
    assert any("post-validation" in warning for warning in result.warnings)


def test_validate_ai_analysis_rejects_when_no_valid_tp(sample_signal, sample_market):
    analysis = AIAnalysis(
        confidence=0.8,
        recommendation="execute",
        reasoning="ok",
        suggested_stop_loss=49000.0,
        suggested_tp1=48000.0,
    )

    result = validate_ai_analysis_against_signal(sample_signal, sample_market, analysis)

    assert result.recommendation == "reject"
    assert "No valid take-profit" in result.reasoning

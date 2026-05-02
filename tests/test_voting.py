"""Tests for AI voting functionality."""
import pytest

from ai_analyzer import TrailingStopMode, _aggregate_voting_results, _parse_model_id


class TestParseModelId:
    def test_parse_openai_format(self):
        provider, model = _parse_model_id("openai/gpt-4o")
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_parse_deepseek_format(self):
        provider, model = _parse_model_id("deepseek/deepseek-chat")
        assert provider == "deepseek"
        assert model == "deepseek-chat"

    def test_parse_anthropic_format(self):
        provider, model = _parse_model_id("anthropic/claude-3-5-sonnet-20241022")
        assert provider == "anthropic"
        assert model == "claude-3-5-sonnet-20241022"

    def test_parse_google_format(self):
        provider, model = _parse_model_id("google/gemini-2.0-flash")
        assert provider == "google"
        assert model == "gemini-2.0-flash"

    def test_parse_ollama_format(self):
        provider, model = _parse_model_id("ollama/llama3.2:latest")
        assert provider == "ollama"
        assert model == "llama3.2:latest"

    def test_parse_single_model_id(self):
        provider, model = _parse_model_id("gpt-4")
        assert provider == ""
        assert model == "gpt-4"

    def test_parse_empty_string(self):
        provider, model = _parse_model_id("")
        assert provider == ""
        assert model == ""


class TestAggregateVotingResults:
    def test_weighted_average_same_recommendation(self):
        results = [
            {"action": "buy", "confidence": 0.8, "reason": "Strong bullish"},
            {"action": "buy", "confidence": 0.7, "reason": "Good setup"},
            {"action": "buy", "confidence": 0.9, "reason": "Perfect entry"},
        ]
        weights = {"model1": 0.3, "model2": 0.3, "model3": 0.4}

        final = _aggregate_voting_results(results, weights, "weighted")
        assert final["action"] == "buy"
        assert final["confidence"] > 0.75

    def test_weighted_average_mixed_recommendations(self):
        results = [
            {"action": "buy", "confidence": 0.8},
            {"action": "hold", "confidence": 0.6},
            {"action": "buy", "confidence": 0.7},
        ]
        weights = {"model1": 0.5, "model2": 0.25, "model3": 0.25}

        final = _aggregate_voting_results(results, weights, "weighted")
        assert final["action"] == "buy"

    def test_consensus_all_agree(self):
        results = [
            {"action": "buy", "confidence": 0.8},
            {"action": "buy", "confidence": 0.7},
            {"action": "buy", "confidence": 0.9},
        ]
        weights = {"model1": 0.33, "model2": 0.33, "model3": 0.34}

        final = _aggregate_voting_results(results, weights, "consensus")
        assert final["action"] == "buy"

    def test_consensus_no_agreement_returns_hold(self):
        results = [
            {"action": "buy", "confidence": 0.8},
            {"action": "sell", "confidence": 0.7},
            {"action": "hold", "confidence": 0.9},
        ]
        weights = {"model1": 0.33, "model2": 0.33, "model3": 0.34}

        final = _aggregate_voting_results(results, weights, "consensus")
        assert final["action"] == "hold"

    def test_best_confidence_highest_confidence(self):
        results = [
            {"action": "buy", "confidence": 0.6, "reason": "Weak signal"},
            {"action": "sell", "confidence": 0.95, "reason": "Strong bearish"},
            {"action": "hold", "confidence": 0.7, "reason": "Neutral"},
        ]
        weights = {"model1": 0.33, "model2": 0.33, "model3": 0.34}

        final = _aggregate_voting_results(results, weights, "best_confidence")
        assert final["action"] == "sell"
        assert final["confidence"] == 0.95

    def test_empty_results_returns_hold(self):
        results = []
        weights = {}

        final = _aggregate_voting_results(results, weights, "weighted")
        assert final["action"] == "hold"
        assert final["confidence"] == 0.0

    def test_weight_normalization(self):
        results = [
            {"action": "buy", "confidence": 0.8},
            {"action": "buy", "confidence": 0.7},
        ]
        weights = {"model1": 1.0, "model2": 1.0}  # Total = 2.0, should normalize

        final = _aggregate_voting_results(results, weights, "weighted")
        assert final["action"] == "buy"
        assert final["confidence"] == pytest.approx(0.75, rel=0.01)


class TestTrailingStopMode:
    def test_mode_values(self):
        assert TrailingStopMode.NONE.value == "none"
        assert TrailingStopMode.AUTO.value == "auto"
        assert TrailingStopMode.MOVING.value == "moving"
        assert TrailingStopMode.BREAKEVEN_ON_TP1.value == "breakeven_on_tp1"
        assert TrailingStopMode.STEP_TRAILING.value == "step_trailing"
        assert TrailingStopMode.PROFIT_PCT_TRAILING.value == "profit_pct_trailing"

    def test_mode_from_string(self):
        mode = TrailingStopMode("breakeven_on_tp1")
        assert mode == TrailingStopMode.BREAKEVEN_ON_TP1

        mode_auto = TrailingStopMode("auto")
        assert mode_auto == TrailingStopMode.AUTO

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            TrailingStopMode("invalid_mode")

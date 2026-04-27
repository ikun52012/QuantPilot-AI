"""
AI API cost tracking.
Records token usage and estimated cost per provider call.
Thread-safe, in-memory with periodic summary logging.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# Approximate cost per 1M tokens (input/output) as of 2025
_COST_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
    "mistral-large-latest": (2.00, 6.00),
    "mistral-small-latest": (0.20, 0.60),
    "codestral-latest": (0.30, 0.90),
}


@dataclass
class UsageRecord:
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)


class AICostTracker:
    """Tracks AI API usage and costs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: list[UsageRecord] = []
        self._totals: dict[str, dict] = {}  # provider -> {calls, tokens, cost}

    def record(
        self,
        provider: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> UsageRecord:
        """Record a single API call's token usage."""
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens

        cost = self._estimate_cost(model, prompt_tokens, completion_tokens)

        rec = UsageRecord(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost,
        )

        with self._lock:
            self._records.append(rec)
            # Keep only last 10000 records
            if len(self._records) > 10000:
                self._records = self._records[-5000:]

            totals = self._totals.setdefault(provider, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
            totals["calls"] += 1
            totals["tokens"] += total_tokens
            totals["cost_usd"] += cost

        if cost > 0:
            logger.debug(
                f"[AI/Cost] {provider}/{model}: {total_tokens} tokens, "
                f"~${cost:.4f}"
            )

        return rec

    def _estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost based on known pricing."""
        model_lower = model.lower()
        for key, (input_cost, output_cost) in _COST_PER_1M.items():
            if key in model_lower:
                return (prompt_tokens * input_cost + completion_tokens * output_cost) / 1_000_000
        return 0.0

    def get_summary(self) -> dict:
        """Get usage summary by provider."""
        with self._lock:
            return {
                "by_provider": dict(self._totals),
                "total_calls": sum(t["calls"] for t in self._totals.values()),
                "total_tokens": sum(t["tokens"] for t in self._totals.values()),
                "total_cost_usd": round(sum(t["cost_usd"] for t in self._totals.values()), 4),
            }

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Get recent usage records."""
        with self._lock:
            records = self._records[-limit:]
        return [
            {
                "provider": r.provider,
                "model": r.model,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "estimated_cost_usd": round(r.estimated_cost_usd, 6),
                "timestamp": r.timestamp,
            }
            for r in reversed(records)
        ]

    def reset(self) -> None:
        """Reset all tracking data."""
        with self._lock:
            self._records.clear()
            self._totals.clear()


# Global tracker instance
ai_costs = AICostTracker()


def extract_usage_from_response(data: dict) -> tuple[int, int, int]:
    """Extract token usage from an OpenAI-compatible API response."""
    usage = data.get("usage") or {}
    return (
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )

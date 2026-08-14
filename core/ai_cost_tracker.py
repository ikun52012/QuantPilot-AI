"""
AI API cost tracking.
Records provider request attempts, token usage and estimated cost by source.
Events are appended to JSONL so budgets and usage survive process restarts.
"""
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

# Approximate cost per 1M tokens (input/output) as of 2025
_COST_PER_1M: dict[str, tuple[float, float]] = {
    # OpenAI Models
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-5.5": (10.00, 30.00),
    "gpt-5.4": (2.50, 10.00),
    "gpt-5.4-mini": (0.15, 0.60),
    "claude-opus-4-7": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "deepseek-v4-pro": (2.00, 8.00),
    "deepseek-v4-flash": (0.10, 0.40),
    # Anthropic Models
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-sonnet-20240229": (3.00, 15.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    # DeepSeek Models
    "deepseek-chat": (1.00, 2.00),
    "deepseek-coder": (1.00, 2.00),
    "deepseek-reasoner": (0.55, 2.19),
    # Mistral Models
    "mistral-large-latest": (2.00, 6.00),
    "mistral-small-latest": (0.20, 0.60),
    "codestral-latest": (0.30, 0.90),
}

_USAGE_SOURCE: ContextVar[str] = ContextVar("ai_usage_source", default="unknown")
_USAGE_USER_ID: ContextVar[str] = ContextVar("ai_usage_user_id", default="")


class AIBudgetExceeded(RuntimeError):
    """Raised before a provider request would exceed a configured daily cap."""


def set_ai_usage_context(source: str, user_id: str = "") -> tuple[Token, Token]:
    return (
        _USAGE_SOURCE.set(str(source or "unknown")),
        _USAGE_USER_ID.set(str(user_id or "")),
    )


def reset_ai_usage_context(tokens: tuple[Token, Token]) -> None:
    source_token, user_token = tokens
    _USAGE_SOURCE.reset(source_token)
    _USAGE_USER_ID.reset(user_token)


@dataclass
class UsageRecord:
    event_type: str = "usage"
    source: str = "unknown"
    user_id: str = ""
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
        self._source_totals: dict[str, dict] = {}
        self._attempts_today: dict[str, int] = {}
        self._date_key = datetime.now(UTC).date().isoformat()
        from core.config import DATA_DIR

        self._default_journal_path = DATA_DIR / "ai_usage.jsonl"
        self._journal_path = self._default_journal_path
        self._load_journal()

    def _refresh_day_locked(self) -> None:
        current = datetime.now(UTC).date().isoformat()
        if current != self._date_key:
            self._date_key = current
            self._attempts_today.clear()

    def _source_limit(self, source: str) -> int:
        from core.config import settings

        if source == "auto_scanner":
            return int(settings.scanner.max_ai_calls_per_day or 0)
        if source == "tradingview":
            return int(settings.ai.webhook_max_provider_requests_per_day or 0)
        return 0

    @staticmethod
    def _global_limit() -> int:
        from core.config import settings

        return int(settings.ai.max_provider_requests_per_day or 0)

    def _journal_enabled(self) -> bool:
        return not (
            "pytest" in sys.modules
            and self._journal_path == self._default_journal_path
        )

    @contextmanager
    def _journal_file_lock(self):
        """Serialize budget reservations across worker processes."""
        if not self._journal_enabled():
            yield
            return
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._journal_path.with_name(f".{self._journal_path.name}.lock")
        deadline = time.monotonic() + 5.0
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 30.0:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise AIBudgetExceeded(
                        "AI request blocked because the cross-process budget lock is unavailable"
                    ) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                os.close(descriptor)
            finally:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _attempt_counts_from_journal(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        if not self._journal_enabled() or not self._journal_path.exists():
            return counts
        try:
            lines = self._journal_path.read_text(encoding="utf-8").splitlines()[-10000:]
        except OSError as exc:
            raise AIBudgetExceeded(
                f"AI request blocked because usage journal could not be read: {exc}"
            ) from exc
        for line in lines:
            try:
                item = json.loads(line)
                timestamp = float(item.get("timestamp") or 0.0)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if str(item.get("event_type") or "") != "attempt" or timestamp <= 0:
                continue
            event_day = datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()
            if event_day != self._date_key:
                continue
            source = str(item.get("source") or "unknown")
            counts[source] = counts.get(source, 0) + 1
        return counts

    def _append_event_unlocked(
        self,
        payload: dict,
        *,
        fail_closed: bool = False,
    ) -> None:
        if not self._journal_enabled():
            return
        try:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            if self._journal_path.exists() and self._journal_path.stat().st_size > 10_000_000:
                lines = self._journal_path.read_text(encoding="utf-8").splitlines()[-5000:]
                self._journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self._journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning(f"[AI/Cost] Could not persist usage event: {exc}")
            if fail_closed:
                raise AIBudgetExceeded(
                    f"AI request blocked because usage reservation could not be persisted: {exc}"
                ) from exc

    def _append_event_locked(self, payload: dict) -> None:
        with self._journal_file_lock():
            self._append_event_unlocked(payload)

    def _load_journal(self) -> None:
        if "pytest" in sys.modules and self._journal_path == self._default_journal_path:
            return
        if not self._journal_path.exists():
            return
        try:
            lines = self._journal_path.read_text(encoding="utf-8").splitlines()[-10000:]
        except OSError as exc:
            logger.warning(f"[AI/Cost] Could not load usage journal: {exc}")
            return
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = str(item.get("event_type") or "usage")
            source = str(item.get("source") or "unknown")
            provider = str(item.get("provider") or "unknown")
            timestamp = float(item.get("timestamp") or 0.0)
            event_day = datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat() if timestamp > 0 else ""
            if event_type == "attempt":
                if event_day == self._date_key:
                    self._attempts_today[source] = self._attempts_today.get(source, 0) + 1
                totals = self._totals.setdefault(
                    provider, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
                )
                totals["requests"] += 1
                source_totals = self._source_totals.setdefault(
                    source, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
                )
                source_totals["requests"] += 1
                continue
            prompt_tokens = int(item.get("prompt_tokens") or 0)
            completion_tokens = int(item.get("completion_tokens") or 0)
            total_tokens = int(item.get("total_tokens") or prompt_tokens + completion_tokens)
            cost = float(item.get("estimated_cost_usd") or 0.0)
            totals = self._totals.setdefault(
                provider, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            totals["calls"] += 1
            totals["tokens"] += total_tokens
            totals["cost_usd"] += cost
            source_totals = self._source_totals.setdefault(
                source, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            source_totals["calls"] += 1
            source_totals["tokens"] += total_tokens
            source_totals["cost_usd"] += cost

    def record_attempt(self, provider: str, model: str) -> None:
        """Reserve one real provider request against persistent daily budgets."""
        source = _USAGE_SOURCE.get()
        user_id = _USAGE_USER_ID.get()
        now = time.time()
        with self._lock:
            self._refresh_day_locked()
            with self._journal_file_lock():
                if self._journal_enabled():
                    self._attempts_today = self._attempt_counts_from_journal()
                source_limit = self._source_limit(source)
                source_used = int(self._attempts_today.get(source, 0))
                global_limit = self._global_limit()
                global_used = sum(self._attempts_today.values())
                if global_limit > 0 and global_used >= global_limit:
                    raise AIBudgetExceeded(
                        f"Global daily AI provider request budget exhausted: "
                        f"{global_used}/{global_limit}"
                    )
                if source_limit > 0 and source_used >= source_limit:
                    raise AIBudgetExceeded(
                        f"Daily AI provider request budget exhausted for "
                        f"{source}: {source_used}/{source_limit}"
                    )
                self._append_event_unlocked(
                    {
                        "event_type": "attempt",
                        "source": source,
                        "user_id": user_id,
                        "provider": provider,
                        "model": model,
                        "timestamp": now,
                    },
                    fail_closed=True,
                )
                self._attempts_today[source] = source_used + 1
                totals = self._totals.setdefault(
                    provider, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
                )
                totals["requests"] += 1
                source_totals = self._source_totals.setdefault(
                    source, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
                )
                source_totals["requests"] += 1

    def requests_today(self, source: str) -> int:
        with self._lock:
            self._refresh_day_locked()
            if self._journal_enabled():
                with self._journal_file_lock():
                    self._attempts_today = self._attempt_counts_from_journal()
            return int(self._attempts_today.get(str(source or "unknown"), 0))

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
        source = _USAGE_SOURCE.get()
        user_id = _USAGE_USER_ID.get()

        rec = UsageRecord(
            event_type="usage",
            source=source,
            user_id=user_id,
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

            totals = self._totals.setdefault(
                provider, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            totals["calls"] += 1
            totals["tokens"] += total_tokens
            totals["cost_usd"] += cost
            source_totals = self._source_totals.setdefault(
                source, {"requests": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            source_totals["calls"] += 1
            source_totals["tokens"] += total_tokens
            source_totals["cost_usd"] += cost
            self._append_event_locked({
                "event_type": "usage",
                "source": source,
                "user_id": user_id,
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": cost,
                "timestamp": rec.timestamp,
            })

        if cost > 0:
            logger.debug(
                f"[AI/Cost] {provider}/{model}: {total_tokens} tokens, "
                f"~${cost:.4f}"
            )

        return rec

    def _estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost based on known pricing."""
        model_lower = model.lower()
        for key, (input_cost, output_cost) in sorted(_COST_PER_1M.items(), key=lambda item: len(item[0]), reverse=True):
            if key in model_lower:
                return (prompt_tokens * input_cost + completion_tokens * output_cost) / 1_000_000
        return 0.0

    def get_summary(self) -> dict:
        """Get usage summary by provider."""
        with self._lock:
            return {
                "by_provider": dict(self._totals),
                "by_source": dict(self._source_totals),
                "requests_today_by_source": dict(self._attempts_today),
                "requests_today_global": sum(self._attempts_today.values()),
                "total_requests": sum(t.get("requests", 0) for t in self._totals.values()),
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
                "source": r.source,
                "user_id": r.user_id,
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
            self._source_totals.clear()
            self._attempts_today.clear()


# Global tracker instance
ai_costs = AICostTracker()


def extract_usage_from_response(data: dict) -> tuple[int, int, int]:
    """Extract token usage from OpenAI- or Anthropic-compatible responses."""
    usage = data.get("usage") or {}
    if "input_tokens" in usage or "output_tokens" in usage:
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)
        return prompt_tokens, completion_tokens, prompt_tokens + completion_tokens
    return (
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
        int(usage.get("total_tokens", 0) or 0),
    )

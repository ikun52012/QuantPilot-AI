"""Automatic market scanner service."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import desc, select

from core.config import settings
from core.database import (
    PositionModel,
    ScannerAuditModel,
    acquire_scanner_setup_lock,
    db_manager,
    get_or_create_scanner_state,
    record_scanner_audit,
    scanner_symbol_on_cooldown,
    set_scanner_symbol_cooldown,
    update_scanner_state_counts,
)
from core.utils.common import position_symbol_key
from core.utils.datetime import utcnow
from services.scanner_learning import compute_outcome_summary, compute_walk_forward_thresholds, sync_scanner_outcomes
from services.scanner_rules import DEFAULT_ENGINE, ScoringContext
from services.signal_processor import SignalProcessor
from services.synthetic_signal import build_synthetic_signal, market_context_from_bundle
from services.unified_ohlcv import OHLCVBundle, UnifiedOHLCVProvider, timeframe_to_seconds

# Lazy import to avoid circular dependency
_bcast_scanner = None
_bcast_tasks: set[asyncio.Task] = set()

def _broadcast_scanner(event: dict) -> None:
    global _bcast_scanner
    if _bcast_scanner is None:
        try:
            from routers.websocket import broadcast_scanner_event
            _bcast_scanner = broadcast_scanner_event
        except Exception:
            return
    try:
        task = asyncio.create_task(_bcast_safe_broadcast(_bcast_scanner, event))
        _bcast_tasks.add(task)
        task.add_done_callback(_bcast_tasks.discard)
    except Exception:
        pass


async def _bcast_safe_broadcast(func, event: dict) -> None:
    """Wrapper to handle exceptions in broadcast tasks."""
    try:
        await func(None, event)
    except Exception as exc:
        logger.debug(f"[Scanner] Broadcast failed: {exc}")


@dataclass
class ScannerUniverseItem:
    watch_symbol: str
    exchange_symbol: str
    target_exchange: str
    target_market_type: str
    source_exchange: str
    source_market_type: str
    source_symbol: str
    universe_source: str
    tradable: bool = True
    tradability_reason: str = ""
    quote_volume: float = 0.0
    liquidity_tier: str = "unknown"
    market_limits: dict[str, Any] = field(default_factory=dict)

    def mapping_overrides(self) -> dict[str, Any]:
        return {
            "exchange_symbol": self.exchange_symbol,
            "target_exchange": self.target_exchange,
            "target_market_type": self.target_market_type,
            "source_exchange": self.source_exchange,
            "source_market_type": self.source_market_type,
            "source_symbol": self.source_symbol,
            "data_source_policy": settings.scanner.data_source_policy,
            "tradable": self.tradable,
            "tradability_reason": self.tradability_reason,
            "universe_source": self.universe_source,
            "liquidity_tier": self.liquidity_tier,
            "quote_volume": self.quote_volume,
        }


@dataclass
class ScannerCandidate:
    watch_symbol: str
    exchange_symbol: str
    exchange_name: str
    market_type: str
    data_source: str
    mapped_asset: bool
    direction: str
    timeframe: str
    current_price: float
    entry_reference: float
    score: float
    setup_type: str
    price_zone: str
    setup_hash: str
    reasons: list[str] = field(default_factory=list)
    indicator_summary: dict[str, Any] = field(default_factory=dict)
    smc_summary: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    fused_timeframes: list[str] = field(default_factory=list)
    fusion_summary: dict[str, Any] = field(default_factory=dict)
    score_breakdown: dict[str, float] = field(default_factory=dict)
    target_exchange: str = ""
    target_market_type: str = ""
    source_exchange: str = ""
    source_market_type: str = ""
    actual_data_source: str = ""
    data_source_policy: str = "fallback"
    tradable: bool = True
    tradability_reason: str = ""
    universe_source: str = "manual"
    liquidity_tier: str = "unknown"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _ohlcv_rows(candles: list[Any]) -> list[list[float]]:
    rows: list[list[float]] = []
    for candle in candles:
        ts = getattr(candle, "timestamp", None)
        if hasattr(ts, "timestamp"):
            ts_ms = int(ts.timestamp() * 1000)
        else:
            ts_ms = 0
        rows.append([
            ts_ms,
            float(getattr(candle, "open", 0.0) or 0.0),
            float(getattr(candle, "high", 0.0) or 0.0),
            float(getattr(candle, "low", 0.0) or 0.0),
            float(getattr(candle, "close", 0.0) or 0.0),
            float(getattr(candle, "volume", 0.0) or 0.0),
        ])
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _zone_distance(price: float, low: float, high: float) -> float:
    bottom = min(low, high)
    top = max(low, high)
    if bottom <= price <= top:
        return 0.0
    return min(abs(price - bottom), abs(price - top))


def _setup_hash(
    *,
    ticker: str,
    direction: str,
    timeframe: str,
    setup_type: str,
    price_zone: str,
    reference_price: float,
) -> str:
    price_bucket = round(reference_price, 2) if reference_price >= 10 else round(reference_price, 5)
    raw = f"{ticker}|{direction}|{timeframe}|{setup_type}|{price_zone}|{price_bucket}"
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


class MarketScannerService:
    """Scans the configured watchlist and injects high-quality candidates."""

    def __init__(self, provider: UnifiedOHLCVProvider | None = None, scope: str = "admin") -> None:
        self.provider = provider or UnifiedOHLCVProvider()
        self.scope = scope
        self._scan_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._last_status: dict[str, Any] = {
            "running": False,
            "last_run_id": "",
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": "",
            "last_summary": {},
            "last_universe": {},
            "source_health": {},
            "live_universe_snapshot": {},
        }
        self._universe_cache: dict[Any, tuple[float, list[ScannerUniverseItem]]] = {}
        self._source_health: dict[str, dict[str, Any]] = {}
        self._live_universe_snapshot: dict[str, Any] = {}
        self._audit_buffer: list[dict[str, Any]] = []
        self._audit_buffer_lock = asyncio.Lock()
        self._adaptive_min_score: float = float(settings.scanner.min_score)
        self._adaptive_min_score_cached_at: float = 0.0
        self._threshold_overrides: dict[str, dict[str, Any]] = {}
        self._learning_summary: dict[str, Any] = {}

    @property
    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)

    async def shutdown(self) -> None:
        self._shutdown_event.set()
        timeout = max(1, int(settings.scanner.shutdown_timeout_secs))
        try:
            await asyncio.wait_for(self._wait_until_idle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[Scanner] Shutdown timed out after {timeout}s; "
                "current scan may still finish in background"
            )

    async def _wait_until_idle(self) -> None:
        while self._last_status.get("running"):
            await asyncio.sleep(0.2)

    async def scan_once(self) -> dict[str, Any]:
        if not settings.scanner.enabled:
            return {"status": "disabled", "reason": "SCANNER_ENABLED=false"}
        if db_manager.async_session_factory is None:
            return {"status": "error", "reason": "Database not initialized (session_factory is None)"}

        if self._scan_lock.locked():
            return {"status": "skipped", "reason": "Scanner already running"}
        await self._scan_lock.acquire()

        try:
            run_id = uuid.uuid4().hex[:12]
            started_at = utcnow()
            self._last_status.update({
                "running": True,
                "last_run_id": run_id,
                "last_started_at": started_at,
                "last_error": "",
            })
            _broadcast_scanner({"event": "scan_start", "run_id": run_id, "started_at": started_at.isoformat()})
            try:
                scan_timeout = max(60, int(settings.scanner.scan_timeout_secs))
                return await asyncio.wait_for(self._scan_once_locked(run_id), timeout=scan_timeout)
            except asyncio.TimeoutError:
                timeout_error = f"Scanner timed out after {scan_timeout}s"
                self._last_status["last_error"] = timeout_error
                logger.error(f"[Scanner] Run {run_id} timed out after {scan_timeout}s")
                await self._audit(run_id, "timeout", reason=timeout_error)
                await self._flush_audit_buffer()
                async with db_manager.async_session_factory() as session:
                    await update_scanner_state_counts(
                        session,
                        self.scope,
                        degraded_mode="timeout",
                        degraded_reason=timeout_error,
                    )
                    await session.commit()
                return {"status": "timeout", "reason": timeout_error, "run_id": run_id}
            except Exception as exc:
                self._last_status["last_error"] = str(exc)
                logger.exception(f"[Scanner] Run {run_id} failed: {exc}")
                await self._audit(run_id, "error", reason=str(exc))
                await self._flush_audit_buffer()
                async with db_manager.async_session_factory() as session:
                    await update_scanner_state_counts(
                        session,
                        self.scope,
                        degraded_mode="error",
                        degraded_reason=str(exc),
                    )
                    await session.commit()
                return {"status": "error", "reason": str(exc), "run_id": run_id}
            finally:
                self._last_status.update({
                    "running": False,
                    "last_finished_at": utcnow(),
                })
        finally:
            self._scan_lock.release()

    async def _scan_once_locked(self, run_id: str) -> dict[str, Any]:
        self._learning_summary = await self._refresh_learning(run_id)
        self._adaptive_min_score = await self._get_effective_min_score()
        self._adaptive_min_score_cached_at = time.time()

        async with db_manager.async_session_factory() as session:
            _ = await update_scanner_state_counts(
                session,
                self.scope,
                scan_delta=1,
                degraded_mode="",
                degraded_reason="",
                mark_scan=True,
            )
            await session.commit()

        scan_results = await self._scan_watchlist_concurrently(run_id)
        candidates: list[tuple[ScannerCandidate, OHLCVBundle]] = []
        scanned = 0
        data_failures = 0
        filtered = 0
        filter_reasons: dict[str, int] = {}
        for item in scan_results:
            scanned += int(item.get("scanned") or 0)
            data_failures += int(item.get("data_failures") or 0)
            filtered += int(item.get("filtered") or 0)
            for reason, count in (item.get("filter_reasons") or {}).items():
                key = str(reason or "unknown")
                filter_reasons[key] = filter_reasons.get(key, 0) + int(count or 0)
            candidates.extend(item.get("candidates") or [])

        candidates, direction_conflicts = await self._resolve_direction_conflicts(run_id, candidates)
        filtered += direction_conflicts
        if direction_conflicts:
            filter_reasons["direction_conflict"] = filter_reasons.get("direction_conflict", 0) + direction_conflicts
        candidates, portfolio_filtered = await self._apply_portfolio_risk_filters(run_id, candidates)
        filtered += portfolio_filtered
        if portfolio_filtered:
            filter_reasons["portfolio_risk"] = filter_reasons.get("portfolio_risk", 0) + portfolio_filtered
        candidates.sort(key=lambda item: item[0].score, reverse=True)
        selected = candidates[: max(1, int(settings.scanner.max_candidates_per_run))]
        processed: list[dict[str, Any]] = []

        if selected:
            max_concurrent = min(3, len(selected))
            ai_semaphore = asyncio.Semaphore(max_concurrent)

            async def process_with_semaphore(candidate: ScannerCandidate, bundle: OHLCVBundle) -> dict[str, Any]:
                async with ai_semaphore:
                    if self._shutdown_event.is_set():
                        return {"status": "skipped", "reason": "shutdown requested"}
                    return await self._dispatch_candidate(run_id, candidate, bundle)

            tasks = [asyncio.create_task(process_with_semaphore(candidate, bundle)) for candidate, bundle in selected]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(f"[Scanner] Parallel dispatch failed: {result}")
                    processed.append({"status": "error", "reason": str(result)})
                else:
                    processed.append(result)

        if data_failures and data_failures >= max(2, scanned // 2):
            degraded_mode = "observe_only"
            degraded_reason = f"{data_failures}/{scanned} symbols failed data quality"
        elif data_failures > 0:
            degraded_mode = "partial_data_failure"
            degraded_reason = f"{data_failures}/{scanned} symbols failed data quality"
        else:
            degraded_mode = ""
            degraded_reason = ""

        async with db_manager.async_session_factory() as session:
            await update_scanner_state_counts(
                session,
                self.scope,
                data_failure_delta=1 if data_failures else 0,
                reset_data_failure_streak=data_failures == 0,
                degraded_mode=degraded_mode,
                degraded_reason=degraded_reason,
            )
            await session.commit()

        funnel = self._build_run_funnel(
            scanned=scanned,
            data_failures=data_failures,
            filtered=filtered,
            direction_conflicts=direction_conflicts,
            candidates=len(candidates),
            selected=len(selected),
            processed=processed,
            filter_reasons=filter_reasons,
        )
        await self._audit(run_id, "run_summary", reason="scanner run summary", payload=funnel)
        await self._flush_audit_buffer()
        summary = {
            "status": "ok",
            "run_id": run_id,
            "mode": settings.scanner.mode,
            "scanned": scanned,
            "filtered": filtered,
            "candidates": len(candidates),
            "selected": len(selected),
            "processed": processed,
            "funnel": funnel,
            "learning": self._learning_summary,
        }
        self._last_status["last_summary"] = summary
        _broadcast_scanner({
            "event": "scan_complete",
            "run_id": run_id,
            "summary": summary,
        })
        return summary

    @staticmethod
    def _scanner_market_type(value: str | None, default: str | None = None) -> str:
        normalized = str(value or default or settings.exchange.market_type or "contract").lower().strip()
        if normalized == "spot":
            return "spot"
        return "contract"

    @staticmethod
    def _compact_market_symbol(symbol: str, market: dict[str, Any] | None = None) -> str:
        if isinstance(market, dict):
            base = str(market.get("base") or "").upper().strip()
            quote = str(market.get("quote") or "").upper().strip()
            if base and quote:
                return f"{base}{quote}"
        text = str(symbol or "").upper().strip()
        if ":" in text:
            text = text.split(":", 1)[0]
        return text.replace("/", "").replace("-", "").replace("_", "")

    @staticmethod
    def _quote_volume_from_ticker(ticker: Any) -> float:
        if not isinstance(ticker, dict):
            return 0.0
        for key in ("quoteVolume", "quote_volume", "baseVolume"):
            try:
                value = float(ticker.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        info = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
        for key in ("quoteVolume", "quote_volume", "turnover24h", "volCcy24h", "volume24h"):
            try:
                value = float(info.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _liquidity_tier(symbol: str, quote_volume: float) -> str:
        text = str(symbol or "").upper().replace("/", "").replace(":USDT", "")
        base = text
        for suffix in ("USDT", "USDC", "BUSD", "USD"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base in {"BTC", "ETH"}:
            return "major"
        if quote_volume >= 500_000_000:
            return "major"
        if quote_volume >= 100_000_000:
            return "high_volume_alt"
        if quote_volume >= 10_000_000:
            return "mid_volume_alt"
        if quote_volume > 0:
            return "long_tail"
        return "unknown"

    @staticmethod
    def _liquidity_tier_threshold_bump(tier: str, universe_source: str) -> float:
        if str(universe_source or "manual").lower().strip() == "manual":
            return 0.0
        return {
            "mid_volume_alt": 2.5,
            "long_tail": 5.0,
        }.get(str(tier or "").lower(), 0.0)

    @staticmethod
    def _clone_universe(universe: list[ScannerUniverseItem]) -> list[ScannerUniverseItem]:
        return [ScannerUniverseItem(**asdict(item)) for item in universe]

    @staticmethod
    def _market_limits_from_market(exchange_id: str, market_symbol: str, market: dict[str, Any]) -> dict[str, Any]:
        limits = market.get("limits", {}) if isinstance(market.get("limits"), dict) else {}
        precision = market.get("precision", {}) if isinstance(market.get("precision"), dict) else {}
        return {
            "symbol": market.get("symbol") or market_symbol,
            "exchange": exchange_id,
            "min_amount": _safe_float((limits.get("amount") or {}).get("min")),
            "min_cost": _safe_float((limits.get("cost") or {}).get("min")),
            "amount_precision": precision.get("amount"),
            "price_precision": precision.get("price"),
            "contract_size": _safe_float(market.get("contractSize"), 1.0),
        }

    def _source_health_key(self, exchange_id: str, market_type: str) -> str:
        return f"{str(exchange_id or '').lower().strip()}:{self._scanner_market_type(market_type)}"

    def _record_source_health(
        self,
        exchange_id: str,
        market_type: str,
        *,
        ok: bool,
        latency_ms: float = 0.0,
        reason: str = "",
        items: int = 0,
        cached: bool = False,
    ) -> None:
        key = self._source_health_key(exchange_id, market_type)
        entry = dict(self._source_health.get(key) or {})
        entry.setdefault("success_count", 0)
        entry.setdefault("failure_count", 0)
        entry.update({
            "exchange": str(exchange_id or "").lower().strip(),
            "market_type": self._scanner_market_type(market_type),
            "last_latency_ms": round(float(latency_ms or 0.0), 2),
            "last_reason": reason,
            "last_items": int(items or 0),
            "last_cached": bool(cached),
            "updated_at": utcnow().isoformat(),
        })
        if ok:
            entry["success_count"] = int(entry.get("success_count") or 0) + 1
            entry["last_success_at"] = entry["updated_at"]
            entry["status"] = "cached" if cached else "ok"
            entry["last_error"] = ""
        else:
            entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
            entry["last_failure_at"] = entry["updated_at"]
            entry["last_error"] = reason
            entry["status"] = "rate_limited" if any(token in reason.lower() for token in ("rate", "429")) else "degraded"
        self._source_health[key] = entry
        self._last_status["source_health"] = self.source_health

    @property
    def source_health(self) -> dict[str, Any]:
        return {key: dict(value) for key, value in self._source_health.items()}

    def clear_universe_cache(self) -> None:
        self._universe_cache.clear()

    async def preview_universe(self, *, force_refresh: bool = False, limit: int = 200) -> dict[str, Any]:
        universe = await self._build_effective_universe(force_refresh=force_refresh, include_untradable=True)
        tradable = [item for item in universe if item.tradable]
        skipped = [item for item in universe if not item.tradable]
        summary = self._universe_summary(tradable)
        summary["preview_count"] = len(universe)
        summary["skipped_count"] = len(skipped)
        summary["source_health"] = self.source_health
        return {
            "summary": summary,
            "items": [asdict(item) for item in tradable[: max(1, int(limit))]],
            "skipped": [asdict(item) for item in skipped[: max(1, int(limit))]],
        }

    def _set_live_universe_snapshot(self, universe: list[ScannerUniverseItem]) -> None:
        symbols = sorted({item.exchange_symbol.upper().strip() for item in universe if item.tradable})
        watch_symbols = sorted({item.watch_symbol.upper().strip() for item in universe if item.tradable})
        snapshot = {
            "created_at": utcnow().isoformat(),
            "count": len(symbols),
            "symbols": symbols,
            "watch_symbols": watch_symbols,
            "target_exchange": str(settings.exchange.name or "").lower().strip(),
            "target_market_type": self._scanner_market_type(settings.exchange.market_type),
        }
        self._live_universe_snapshot = snapshot
        self._last_status["live_universe_snapshot"] = snapshot

    def _coerce_universe_item(self, symbol: str | ScannerUniverseItem) -> ScannerUniverseItem:
        if isinstance(symbol, ScannerUniverseItem):
            return symbol
        watch = str(symbol or "").upper().strip()
        target_exchange = str(settings.exchange.name or "").lower().strip()
        target_market_type = self._scanner_market_type(settings.exchange.market_type)
        source_exchange = str(settings.scanner.source_exchange or target_exchange).lower().strip()
        source_market_type = self._scanner_market_type(settings.scanner.source_market_type, target_market_type)
        return ScannerUniverseItem(
            watch_symbol=watch,
            exchange_symbol=watch,
            target_exchange=target_exchange,
            target_market_type=target_market_type,
            source_exchange=source_exchange,
            source_market_type=source_market_type,
            source_symbol=watch,
            universe_source="manual",
        )

    def _manual_universe_item(self, symbol: str, universe_source: str = "manual") -> ScannerUniverseItem:
        item = self._coerce_universe_item(symbol)
        item.universe_source = universe_source
        return item

    def _universe_summary(self, universe: list[ScannerUniverseItem]) -> dict[str, Any]:
        target_exchange = str(settings.exchange.name or "").lower().strip()
        target_market_type = self._scanner_market_type(settings.exchange.market_type)
        source_exchange = str(settings.scanner.source_exchange or target_exchange).lower().strip()
        source_market_type = self._scanner_market_type(settings.scanner.source_market_type, target_market_type)
        return {
            "source_mode": settings.scanner.source_mode,
            "data_source_policy": settings.scanner.data_source_policy,
            "target_exchange": target_exchange,
            "target_market_type": target_market_type,
            "source_exchange": source_exchange,
            "source_market_type": source_market_type,
            "universe_top_n": settings.scanner.universe_top_n,
            "universe_min_quote_volume": settings.scanner.universe_min_quote_volume,
            "universe_cache_ttl_secs": settings.scanner.universe_cache_ttl_secs,
            "watchlist_empty_means_all_tradable": True,
            "live_whitelist_empty_means_all_tradable": True,
            "include_symbols": list(settings.scanner.include_symbols or []),
            "exclude_symbols": list(settings.scanner.exclude_symbols or []),
            "count": len(universe),
            "tradable_count": sum(1 for item in universe if item.tradable),
            "source_health": self.source_health,
            "symbols": [item.watch_symbol for item in universe[:50]],
        }

    async def _build_effective_universe(
        self,
        run_id: str = "",
        *,
        force_refresh: bool = False,
        include_untradable: bool = False,
    ) -> list[ScannerUniverseItem]:
        mode = str(settings.scanner.source_mode or "manual").lower().strip()
        target_exchange = str(settings.exchange.name or "").lower().strip()
        target_market_type = self._scanner_market_type(settings.exchange.market_type)
        source_exchange = str(settings.scanner.source_exchange or target_exchange).lower().strip()
        source_market_type = self._scanner_market_type(settings.scanner.source_market_type, target_market_type)
        universe: list[ScannerUniverseItem] = []

        if mode == "manual" and settings.scanner.watchlist:
            universe = [self._manual_universe_item(symbol, "manual") for symbol in settings.scanner.watchlist]
        else:
            auto_source_exchange = target_exchange if mode in {"manual", "follow_exchange"} else source_exchange
            auto_source_market_type = target_market_type if mode in {"manual", "follow_exchange"} else source_market_type
            universe_source = "manual_all_tradable" if mode == "manual" else mode
            try:
                universe = await self._fetch_exchange_universe(
                    source_exchange=auto_source_exchange,
                    source_market_type=auto_source_market_type,
                    target_exchange=target_exchange,
                    target_market_type=target_market_type,
                    universe_source=universe_source,
                    force_refresh=force_refresh,
                    include_untradable=include_untradable,
                )
            except Exception as exc:
                logger.warning(f"[Scanner] Universe build failed for {auto_source_exchange}/{auto_source_market_type}: {exc}")
                if run_id:
                    await self._audit(
                        run_id,
                        "universe_error",
                        reason=str(exc),
                        payload={
                            "source_mode": mode,
                            "source_exchange": auto_source_exchange,
                            "source_market_type": auto_source_market_type,
                            "target_exchange": target_exchange,
                            "target_market_type": target_market_type,
                        },
                    )
                universe = []
            if mode == "hybrid":
                manual_symbols = [*list(settings.scanner.watchlist or []), *list(settings.scanner.include_symbols or [])]
                universe.extend(self._manual_universe_item(symbol, "hybrid_manual") for symbol in manual_symbols)

        excludes = {str(symbol or "").upper().strip() for symbol in settings.scanner.exclude_symbols or []}
        deduped: dict[str, ScannerUniverseItem] = {}
        for item in universe:
            key = item.watch_symbol.upper().strip()
            if not key or key in excludes or item.exchange_symbol.upper().strip() in excludes:
                continue
            if not include_untradable and not item.tradable:
                continue
            deduped.setdefault(key, item)
        result = list(deduped.values())
        if run_id:
            await self._audit(run_id, "universe", reason="effective scanner universe", payload=self._universe_summary(result))
        return result

    async def _fetch_exchange_universe(
        self,
        *,
        source_exchange: str,
        source_market_type: str,
        target_exchange: str,
        target_market_type: str,
        universe_source: str,
        force_refresh: bool = False,
        include_untradable: bool = False,
    ) -> list[ScannerUniverseItem]:
        source_type = self._scanner_market_type(source_market_type)
        target_type = self._scanner_market_type(target_market_type)
        cache_key = (
            str(source_exchange or "").lower().strip(),
            source_type,
            str(target_exchange or "").lower().strip(),
            target_type,
            universe_source,
            int(settings.scanner.universe_top_n or 1),
            float(settings.scanner.universe_min_quote_volume or 0.0),
            bool(include_untradable),
        )
        ttl = max(0, int(settings.scanner.universe_cache_ttl_secs or 0))
        now = time.monotonic()
        cached = self._universe_cache.get(cache_key)
        if not force_refresh and ttl > 0 and cached and now - cached[0] <= ttl:
            universe = self._clone_universe(cached[1])
            self._record_source_health(
                source_exchange,
                source_type,
                ok=True,
                items=len(universe),
                cached=True,
            )
            return universe

        started = time.monotonic()
        try:
            universe = await asyncio.to_thread(
                self._fetch_exchange_universe_sync,
                source_exchange,
                source_type,
                target_exchange,
                target_type,
                universe_source,
                include_untradable,
            )
            latency_ms = (time.monotonic() - started) * 1000.0
            self._record_source_health(source_exchange, source_type, ok=True, latency_ms=latency_ms, items=len(universe))
            if ttl > 0:
                self._universe_cache[cache_key] = (time.monotonic(), self._clone_universe(universe))
            return universe
        except Exception as exc:
            latency_ms = (time.monotonic() - started) * 1000.0
            self._record_source_health(source_exchange, source_type, ok=False, latency_ms=latency_ms, reason=str(exc))
            raise

    def _fetch_exchange_universe_sync(
        self,
        source_exchange: str,
        source_market_type: str,
        target_exchange: str,
        target_market_type: str,
        universe_source: str,
        include_untradable: bool = False,
    ) -> list[ScannerUniverseItem]:
        from exchange import _get_or_create_exchange, _market_matches_type, _market_type_key, _symbol_candidates

        source_exchange = str(source_exchange or settings.exchange.name).lower().strip()
        target_exchange = str(target_exchange or settings.exchange.name).lower().strip()
        source_type = self._scanner_market_type(source_market_type)
        target_type = self._scanner_market_type(target_market_type)
        source_family = _market_type_key(source_type)
        target_family = _market_type_key(target_type)
        exchange = _get_or_create_exchange(exchange_id=source_exchange, live=False, sandbox=False, market_type=source_type)
        source_markets = exchange.load_markets()
        target_markets = source_markets
        if source_exchange != target_exchange or source_type != target_type:
            target = _get_or_create_exchange(exchange_id=target_exchange, live=False, sandbox=False, market_type=target_type)
            target_markets = target.load_markets()

        tickers: dict[str, Any] = {}
        try:
            fetch_tickers = getattr(exchange, "fetch_tickers", None)
            if callable(fetch_tickers):
                tickers = fetch_tickers() or {}
        except Exception as exc:
            logger.debug(f"[Scanner] fetch_tickers unavailable for universe on {source_exchange}: {exc}")

        min_quote_volume = float(settings.scanner.universe_min_quote_volume or 0.0)
        rows: list[tuple[float, ScannerUniverseItem]] = []
        for market_symbol, market in source_markets.items():
            if not isinstance(market, dict):
                continue
            if market.get("active") is False:
                continue
            if not _market_matches_type(market, source_family):
                continue
            quote = str(market.get("quote") or "").upper().strip()
            if quote and quote not in {"USDT", "USDC", "USD", "BUSD"}:
                continue
            watch_symbol = self._compact_market_symbol(str(market_symbol), market)
            ticker = tickers.get(str(market_symbol)) or tickers.get(str(market.get("symbol") or "")) or {}
            quote_volume = self._quote_volume_from_ticker(ticker)
            if quote_volume > 0 and quote_volume < min_quote_volume:
                if include_untradable:
                    rows.append((
                        quote_volume,
                        ScannerUniverseItem(
                            watch_symbol=watch_symbol,
                            exchange_symbol=watch_symbol,
                            target_exchange=target_exchange,
                            target_market_type=target_type,
                            source_exchange=source_exchange,
                            source_market_type=source_type,
                            source_symbol=watch_symbol,
                            universe_source=universe_source,
                            tradable=False,
                            tradability_reason="quote_volume_below_universe_minimum",
                            quote_volume=quote_volume,
                            liquidity_tier=self._liquidity_tier(watch_symbol, quote_volume),
                            market_limits=self._market_limits_from_market(source_exchange, str(market_symbol), market),
                        ),
                    ))
                continue

            target_symbol = watch_symbol
            tradable = False
            tradability_reason = "not_found_on_target"
            target_limits: dict[str, Any] = {}
            for candidate in _symbol_candidates(watch_symbol, target_family):
                target_market = target_markets.get(candidate)
                if isinstance(target_market, dict) and _market_matches_type(target_market, target_family):
                    target_symbol = self._compact_market_symbol(candidate, target_market)
                    tradable = True
                    tradability_reason = "available_on_target"
                    target_limits = self._market_limits_from_market(target_exchange, candidate, target_market)
                    break
            if not tradable and source_exchange == target_exchange and source_type == target_type:
                target_symbol = watch_symbol
                tradable = True
                tradability_reason = "available_on_target"
                target_limits = self._market_limits_from_market(target_exchange, str(market_symbol), market)
            if not tradable:
                if not include_untradable:
                    continue
                target_limits = {}

            rows.append((
                quote_volume,
                ScannerUniverseItem(
                    watch_symbol=watch_symbol,
                    exchange_symbol=target_symbol,
                    target_exchange=target_exchange,
                    target_market_type=target_type,
                    source_exchange=source_exchange,
                    source_market_type=source_type,
                    source_symbol=watch_symbol,
                    universe_source=universe_source,
                    tradable=tradable,
                    tradability_reason=tradability_reason,
                    quote_volume=quote_volume,
                    liquidity_tier=self._liquidity_tier(watch_symbol, quote_volume),
                    market_limits=target_limits,
                ),
            ))

        rows.sort(key=lambda item: item[0], reverse=True)
        top_n = max(1, int(settings.scanner.universe_top_n or 1))
        return [item for _, item in rows[:top_n]]

    async def _scan_watchlist_concurrently(self, run_id: str) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(max(1, int(settings.scanner.max_concurrent_fetches)))
        universe = await self._build_effective_universe(run_id)
        self._last_status["last_universe"] = self._universe_summary(universe)
        if str(settings.scanner.mode).lower().strip() == "live":
            self._set_live_universe_snapshot(universe)
        if not universe:
            await self._audit(
                run_id,
                "universe_empty",
                reason="scanner effective universe is empty",
                payload=self._last_status["last_universe"],
            )
            return [{"scanned": 0, "data_failures": 0, "filtered": 0, "filter_reasons": {"universe_empty": 1}, "candidates": []}]

        async def worker(item: ScannerUniverseItem) -> dict[str, Any]:
            async with semaphore:
                if self._shutdown_event.is_set():
                    return {"scanned": 0, "data_failures": 0, "filtered": 0, "filter_reasons": {"shutdown": 1}, "candidates": []}
                return await self._scan_watch_symbol(run_id, item)

        tasks = [asyncio.create_task(worker(item)) for item in universe]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        normalized: list[dict[str, Any]] = []
        for item, result in zip(universe, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[Scanner] Symbol scan failed for {item.watch_symbol}: {result}")
                await self._audit(
                    run_id,
                    "data_error",
                    watch_symbol=item.watch_symbol,
                    exchange_symbol=item.exchange_symbol,
                    reason=str(result),
                    payload={"universe": asdict(item)},
                )
                normalized.append({"scanned": 1, "data_failures": 1, "filtered": 0, "filter_reasons": {"data_error": 1}, "candidates": []})
            else:
                normalized.append(result)

        # Retry transient failures
        retry_candidates = [
            (i, item) for i, (item, result) in enumerate(zip(universe, results, strict=False))
            if isinstance(result, Exception) and self._is_transient_failure(result)
        ]
        if retry_candidates:
            retry_tasks = [
                asyncio.create_task(worker(item))
                for _, item in retry_candidates
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
            for (idx, _), retry_result in zip(retry_candidates, retry_results, strict=False):
                if not isinstance(retry_result, Exception):
                    normalized[idx] = retry_result

        return normalized

    async def _scan_watch_symbol(self, run_id: str, watch_symbol: str | ScannerUniverseItem) -> dict[str, Any]:
        item = self._coerce_universe_item(watch_symbol)
        try:
            bundle = await self._fetch_bundle_with_retry(item)
        except Exception as exc:
            await self._audit(
                run_id,
                "data_error",
                watch_symbol=item.watch_symbol,
                exchange_symbol=item.exchange_symbol,
                reason=str(exc),
                payload={"universe": asdict(item)},
            )
            logger.warning(f"[Scanner] Data fetch failed for {item.watch_symbol}: {exc}")
            return {"scanned": 1, "data_failures": 1, "filtered": 0, "filter_reasons": {"data_error": 1}, "candidates": []}

        mapping = bundle.mapping
        await self._audit(
            run_id,
            "scanned",
            watch_symbol=mapping.watch_symbol,
            exchange_symbol=mapping.exchange_symbol,
            reason="quality_ok" if bundle.quality_passed else ";".join(bundle.quality_reasons),
            payload={
                "quality": bundle.data_quality,
                "data_source": mapping.data_source,
                "target_exchange": mapping.target_exchange or mapping.exchange_name,
                "source_exchange": mapping.source_exchange,
                "actual_data_source": mapping.actual_data_source or bundle.data_quality.get("actual_data_source"),
                "tradable": mapping.tradable,
                "tradability_reason": mapping.tradability_reason,
                "universe_source": mapping.universe_source,
            },
        )

        if not bundle.quality_passed:
            reasons = list(bundle.quality_reasons or ["quality_failed"])
            return {
                "scanned": 1,
                "data_failures": 1,
                "filtered": 1,
                "filter_reasons": {str(reason): 1 for reason in reasons},
                "candidates": [],
            }

        symbol_lock = await self._symbol_cooldown(mapping.exchange_symbol)
        if symbol_lock:
            await self._audit(
                run_id,
                "cooldown",
                watch_symbol=mapping.watch_symbol,
                exchange_symbol=mapping.exchange_symbol,
                reason="symbol cooldown active",
                payload={"expires_at": getattr(symbol_lock, "expires_at", None)},
            )
            return {"scanned": 1, "data_failures": 0, "filtered": 1, "filter_reasons": {"symbol_cooldown": 1}, "candidates": []}

        event_ok, event_reason, event_payload = self._event_session_filter(bundle)
        if not event_ok:
            await self._audit(
                run_id,
                "event_filter",
                watch_symbol=mapping.watch_symbol,
                exchange_symbol=mapping.exchange_symbol,
                reason=event_reason,
                payload=event_payload,
            )
            return {"scanned": 1, "data_failures": 0, "filtered": 1, "filter_reasons": {event_reason or "event_filter": 1}, "candidates": []}

        symbol_candidates = self._build_candidates(bundle)
        if not symbol_candidates:
            await self._audit(
                run_id,
                "filtered",
                watch_symbol=mapping.watch_symbol,
                exchange_symbol=mapping.exchange_symbol,
                reason="no candidate reached pre-scan score",
            )
            return {"scanned": 1, "data_failures": 0, "filtered": 1, "filter_reasons": {"no_candidate": 1}, "candidates": []}

        accepted: list[tuple[ScannerCandidate, OHLCVBundle]] = []
        for candidate in symbol_candidates:
            await self._audit(
                run_id,
                "candidate",
                watch_symbol=candidate.watch_symbol,
                exchange_symbol=candidate.exchange_symbol,
                direction=candidate.direction,
                score=candidate.score,
                setup_hash=candidate.setup_hash,
                reason="candidate accepted by scanner",
                payload=candidate.to_payload(),
            )
            _broadcast_scanner({
                "event": "candidate_found",
                "run_id": run_id,
                "symbol": candidate.exchange_symbol,
                "direction": candidate.direction,
                "score": candidate.score,
                "timeframe": candidate.timeframe,
            })
            accepted.append((candidate, bundle))
        return {"scanned": 1, "data_failures": 0, "filtered": 0, "filter_reasons": {}, "candidates": accepted}

    def _build_candidates(self, bundle: OHLCVBundle) -> list[ScannerCandidate]:
        if settings.scanner.mtf_consensus_enabled:
            return self._build_consensus_candidates(bundle)

        primary_tf = bundle.primary_timeframe
        indicators = bundle.indicators.get(primary_tf, {})
        current = float(bundle.current_price or 0.0)
        if current <= 0:
            return []

        directions: list[str] = []
        for tf_indicators in bundle.indicators.values():
            directions.extend(self._candidate_directions(tf_indicators, current))
        directions = list(dict.fromkeys(directions))

        all_smc: dict[str, dict[str, Any]] = {}
        for direction in directions:
            all_smc[direction] = self._analyze_smc(bundle, direction)

        htf_trend: str | None = None
        if settings.scanner.htf_conflict_enabled:
            tf_list = sorted(bundle.candles.keys(), key=lambda tf: timeframe_to_seconds(tf))
            if len(tf_list) >= 2:
                htf = tf_list[-1]
                for direction in directions:
                    htf_ctx = all_smc.get(direction, {}).get(htf)
                    if htf_ctx:
                        struct = getattr(htf_ctx, "structure", None)
                        htf_trend = str(getattr(struct, "trend", "") or "").lower()
                        break

        candidates: list[ScannerCandidate] = []
        for direction in directions:
            for timeframe, ctx in all_smc[direction].items():
                tf_indicators = bundle.indicators.get(timeframe) or indicators
                candidate = self._score_smc_candidate(
                    bundle, ctx, timeframe, direction, tf_indicators,
                    htf_trend=htf_trend if timeframe != sorted(bundle.candles.keys(), key=lambda tf: timeframe_to_seconds(tf))[-1] else None,
                )
                if candidate:
                    candidates.append(candidate)

        return self._fuse_timeframe_candidates(candidates, bundle)

    def _build_consensus_candidates(self, bundle: OHLCVBundle) -> list[ScannerCandidate]:
        current = float(bundle.current_price or 0.0)
        if current <= 0:
            return []

        tf_list = sorted(bundle.candles.keys(), key=lambda tf: timeframe_to_seconds(tf))
        if not tf_list:
            return []

        direction_items: dict[str, list[ScannerCandidate]] = {"long": [], "short": []}
        direction_scores: dict[str, dict[str, Any]] = {}
        all_smc = {direction: self._analyze_smc(bundle, direction) for direction in ("long", "short")}

        htf_trends: dict[str, str | None] = {}
        htf = tf_list[-1]
        for direction in ("long", "short"):
            htf_ctx = all_smc.get(direction, {}).get(htf)
            if htf_ctx:
                struct = getattr(htf_ctx, "structure", None)
                htf_trends[direction] = str(getattr(struct, "trend", "") or "").lower() or None
            else:
                htf_trends[direction] = None

        for direction in ("long", "short"):
            for timeframe in tf_list:
                ctx = all_smc.get(direction, {}).get(timeframe)
                if not ctx:
                    continue
                tf_indicators = bundle.indicators.get(timeframe) or bundle.indicators.get(bundle.primary_timeframe, {})
                candidate = self._score_smc_candidate(
                    bundle,
                    ctx,
                    timeframe,
                    direction,
                    tf_indicators,
                    htf_trend=htf_trends.get(direction) if timeframe != htf else None,
                )
                if candidate:
                    direction_items[direction].append(candidate)

        for direction, items in direction_items.items():
            direction_scores[direction] = self._consensus_direction_summary(items, tf_list)

        best_direction = max(direction_scores, key=lambda direction: float(direction_scores[direction].get("score") or 0.0))
        opposite_direction = "short" if best_direction == "long" else "long"
        best_score = float(direction_scores[best_direction].get("score") or 0.0)
        opposite_score = float(direction_scores[opposite_direction].get("score") or 0.0)
        margin = best_score - opposite_score
        min_margin = float(settings.scanner.mtf_consensus_min_margin)
        required_confirmations = min(max(1, len(tf_list)), max(1, int(settings.scanner.min_mtf_confirmations)))
        if (
            best_score <= 0
            or margin < min_margin
            or len(direction_items[best_direction]) < required_confirmations
        ):
            return []

        return self._finalize_consensus_candidate(
            bundle=bundle,
            direction=best_direction,
            items=direction_items[best_direction],
            direction_scores=direction_scores,
            margin=margin,
            required_confirmations=required_confirmations,
            tf_list=tf_list,
        )

    def _consensus_timeframe_weight(self, timeframe: str, tf_list: list[str]) -> float:
        if not tf_list:
            return 1.0
        ordered = list(tf_list)
        if timeframe == ordered[-1]:
            return float(settings.scanner.mtf_consensus_htf_weight)
        if timeframe == ordered[0] and len(ordered) > 1:
            return float(settings.scanner.mtf_consensus_ltf_weight)
        return 1.0

    def _consensus_direction_summary(self, items: list[ScannerCandidate], tf_list: list[str]) -> dict[str, Any]:
        if not items:
            return {"score": 0.0, "confirmations": 0, "timeframes": [], "weighted_score": 0.0}
        weighted_total = 0.0
        weight_sum = 0.0
        timeframe_scores: dict[str, float] = {}
        for item in items:
            weight = self._consensus_timeframe_weight(item.timeframe, tf_list)
            weighted_total += float(item.score) * weight
            weight_sum += weight
            timeframe_scores[item.timeframe] = float(item.score)
        confirmations = len(timeframe_scores)
        weighted_score = weighted_total / max(1.0, weight_sum)
        confirmation_bonus = min(12.0, max(0, confirmations - 1) * float(settings.scanner.mtf_confirmation_bonus))
        score = round(max(0.0, min(100.0, weighted_score + confirmation_bonus)), 2)
        return {
            "score": score,
            "weighted_score": round(weighted_score, 2),
            "confirmation_bonus": round(confirmation_bonus, 2),
            "confirmations": confirmations,
            "timeframes": list(timeframe_scores.keys()),
            "timeframe_scores": timeframe_scores,
        }

    def _finalize_consensus_candidate(
        self,
        *,
        bundle: OHLCVBundle,
        direction: str,
        items: list[ScannerCandidate],
        direction_scores: dict[str, dict[str, Any]],
        margin: float,
        required_confirmations: int,
        tf_list: list[str],
    ) -> list[ScannerCandidate]:
        if not items:
            return []
        ordered = sorted(items, key=lambda item: item.score, reverse=True)
        base = ordered[0]
        summary = direction_scores[direction]
        consensus_score = float(summary.get("score") or base.score)
        tier_bump = self._liquidity_tier_threshold_bump(base.liquidity_tier, base.universe_source)
        threshold = self._min_score_for(base.exchange_symbol, "mtf", direction) + tier_bump
        if consensus_score < threshold:
            return []

        base.score = round(consensus_score, 2)
        base.timeframe = "mtf"
        base.fused_timeframes = list(summary.get("timeframes") or [])
        base.reasons.append(
            f"MTF consensus {direction}: score {consensus_score:.1f}, margin {margin:.1f}, "
            f"confirmations {len(base.fused_timeframes)}/{required_confirmations}"
        )
        base.fusion_summary = {
            "enabled": True,
            "mode": "consensus",
            "decision": direction,
            "margin": round(margin, 2),
            "min_margin": float(settings.scanner.mtf_consensus_min_margin),
            "confirmations": len(base.fused_timeframes),
            "required_confirmations": required_confirmations,
            "available_timeframes": tf_list,
            "direction_scores": direction_scores,
            "min_score_threshold": threshold,
            "liquidity_tier_threshold_bump": tier_bump,
            "candidates": [
                {
                    "timeframe": item.timeframe,
                    "score": item.score,
                    "setup_type": item.setup_type,
                    "zone": item.price_zone,
                }
                for item in ordered
            ],
        }
        base.smc_summary["multi_timeframe"] = base.fusion_summary
        base.indicator_summary["multi_timeframe"] = {tf: bundle.indicators.get(tf, {}) for tf in base.fused_timeframes}
        base.quality["min_score_threshold"] = threshold
        base.quality["liquidity_tier_threshold_bump"] = tier_bump
        base.quality["mtf_confirmations_required"] = required_confirmations
        base.quality["mtf_consensus_margin"] = round(margin, 2)
        base.setup_hash = _setup_hash(
            ticker=base.exchange_symbol,
            direction=base.direction,
            timeframe="mtf_consensus:" + ",".join(base.fused_timeframes),
            setup_type=base.setup_type,
            price_zone=base.price_zone,
            reference_price=base.entry_reference,
        )
        return [base]

    def _fuse_timeframe_candidates(
        self,
        candidates: list[ScannerCandidate],
        bundle: OHLCVBundle,
    ) -> list[ScannerCandidate]:
        if not candidates:
            return []

        by_direction: dict[str, list[ScannerCandidate]] = {}
        for candidate in candidates:
            by_direction.setdefault(candidate.direction, []).append(candidate)

        fused: list[ScannerCandidate] = []
        opposite_best = {
            direction: max((item.score for item in items), default=0.0)
            for direction, items in by_direction.items()
        }
        for direction, items in by_direction.items():
            ordered = sorted(items, key=lambda item: item.score, reverse=True)
            base = ordered[0]
            timeframes = list(dict.fromkeys(item.timeframe for item in ordered if item.timeframe))
            confirmations = len(timeframes)
            available_timeframes = max(1, len(bundle.candles or {}))
            required_confirmations = 1
            if settings.scanner.hard_filters_enabled:
                required_confirmations = min(
                    available_timeframes,
                    max(1, int(settings.scanner.min_mtf_confirmations)),
                )
            if confirmations < required_confirmations:
                base.quality.setdefault("hard_filters", {})["mtf_rejected"] = True
                base.quality["mtf_confirmations_required"] = required_confirmations
                continue
            avg_score = sum(item.score for item in ordered) / max(1, len(ordered))
            bonus = max(0.0, confirmations - 1) * float(settings.scanner.mtf_confirmation_bonus)
            bonus = min(18.0, self._weighted("mtf_confirmation", bonus))
            opposite_direction = "short" if direction == "long" else "long"
            conflict_score = opposite_best.get(opposite_direction, 0.0)
            conflict_penalty = 0.0
            if conflict_score and conflict_score >= base.score - 8.0:
                conflict_penalty = self._weighted("conflict_penalty", float(settings.scanner.mtf_conflict_penalty))

            base.score = round(max(0.0, min(100.0, base.score + bonus - conflict_penalty)), 2)
            if confirmations > 1:
                base.reasons.append(f"multi-timeframe confirmation: {', '.join(timeframes)}")
            if conflict_penalty:
                base.reasons.append(f"opposite timeframe pressure penalized ({conflict_score:.1f})")
            tier_bump = self._liquidity_tier_threshold_bump(base.liquidity_tier, base.universe_source)
            threshold = self._min_score_for(base.exchange_symbol, base.timeframe, base.direction) + tier_bump
            base.fused_timeframes = timeframes
            base.fusion_summary = {
                "enabled": True,
                "timeframes": timeframes,
                "confirmations": confirmations,
                "required_confirmations": required_confirmations,
                "avg_score": round(avg_score, 2),
                "bonus": round(bonus, 2),
                "conflict_penalty": round(conflict_penalty, 2),
                "opposite_best_score": round(conflict_score, 2),
                "min_score_threshold": threshold,
                "liquidity_tier_threshold_bump": tier_bump,
                "candidates": [
                    {
                        "timeframe": item.timeframe,
                        "score": item.score,
                        "setup_type": item.setup_type,
                        "zone": item.price_zone,
                    }
                    for item in ordered
                ],
            }
            base.smc_summary["multi_timeframe"] = base.fusion_summary
            base.indicator_summary["multi_timeframe"] = {
                tf: bundle.indicators.get(tf, {}) for tf in timeframes
            }
            base.setup_hash = _setup_hash(
                ticker=base.exchange_symbol,
                direction=base.direction,
                timeframe="mtf:" + ",".join(timeframes),
                setup_type=base.setup_type,
                price_zone=base.price_zone,
                reference_price=base.entry_reference,
            )
            base.quality["min_score_threshold"] = threshold
            base.quality["liquidity_tier_threshold_bump"] = tier_bump
            base.quality["mtf_confirmations_required"] = required_confirmations
            if base.score >= threshold:
                fused.append(base)

        if not fused:
            return []
        best = max(fused, key=lambda item: item.score)
        return [best]

    async def _resolve_direction_conflicts(
        self,
        run_id: str,
        candidates: list[tuple[ScannerCandidate, OHLCVBundle]],
    ) -> tuple[list[tuple[ScannerCandidate, OHLCVBundle]], int]:
        """Keep only the highest-score candidate per exchange symbol for this scan."""
        if not candidates:
            return [], 0

        by_symbol: dict[str, list[tuple[ScannerCandidate, OHLCVBundle]]] = {}
        for candidate, bundle in candidates:
            key = candidate.exchange_symbol.upper().strip() or candidate.watch_symbol.upper().strip()
            by_symbol.setdefault(key, []).append((candidate, bundle))

        kept: list[tuple[ScannerCandidate, OHLCVBundle]] = []
        dropped_count = 0
        for symbol, items in by_symbol.items():
            ordered = sorted(items, key=lambda item: item[0].score, reverse=True)
            winner, winner_bundle = ordered[0]
            kept.append((winner, winner_bundle))
            for loser, _ in ordered[1:]:
                dropped_count += 1
                event_type = "direction_conflict" if loser.direction != winner.direction else "symbol_deduped"
                await self._audit(
                    run_id,
                    event_type,
                    watch_symbol=loser.watch_symbol,
                    exchange_symbol=loser.exchange_symbol,
                    direction=loser.direction,
                    score=loser.score,
                    setup_hash=loser.setup_hash,
                    reason=(
                        f"same scan kept {winner.direction} score {winner.score:.1f}; "
                        f"dropped {loser.direction} score {loser.score:.1f} for {symbol}"
                    ),
                    payload={
                        "kept": {
                            "direction": winner.direction,
                            "score": winner.score,
                            "setup_hash": winner.setup_hash,
                            "timeframe": winner.timeframe,
                            "fused_timeframes": winner.fused_timeframes,
                        },
                        "dropped": {
                            "direction": loser.direction,
                            "score": loser.score,
                            "setup_hash": loser.setup_hash,
                            "timeframe": loser.timeframe,
                            "fused_timeframes": loser.fused_timeframes,
                        },
                    },
                )
        return kept, dropped_count

    def _portfolio_bucket(self, symbol: str) -> str:
        text = str(symbol or "").upper().strip()
        base = text
        for suffix in ("USDT", "USDC", "BUSD", "USD", "/USDT", "/USDC", "/BUSD", "/USD", ":USDT", ":USD", ".P"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        base = base.replace("/", "").replace(":", "")
        for bucket, symbols in (settings.scanner.correlation_buckets or {}).items():
            normalized = {str(item).upper().strip() for item in symbols or []}
            if base in normalized or text in normalized:
                return str(bucket or base).lower()
        return base.lower() or text.lower()

    async def _open_portfolio_exposure_counts(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        if db_manager.async_session_factory is None:
            return counts
        try:
            async with db_manager.async_session_factory() as session:
                result = await session.execute(
                    select(PositionModel).where(PositionModel.status.in_(["open", "pending"]))
                )
                for position in result.scalars().all():
                    direction = str(position.direction or "").lower()
                    if direction not in {"long", "short"}:
                        continue
                    key = (self._portfolio_bucket(position.ticker), direction)
                    counts[key] = counts.get(key, 0) + 1
        except Exception as exc:
            logger.warning(f"[Scanner] Portfolio exposure check failed: {exc}")
        return counts

    async def _apply_portfolio_risk_filters(
        self,
        run_id: str,
        candidates: list[tuple[ScannerCandidate, OHLCVBundle]],
    ) -> tuple[list[tuple[ScannerCandidate, OHLCVBundle]], int]:
        if not settings.scanner.portfolio_risk_enabled or not candidates:
            return candidates, 0

        existing_counts = await self._open_portfolio_exposure_counts()
        in_run_counts: dict[tuple[str, str], int] = {}
        kept: list[tuple[ScannerCandidate, OHLCVBundle]] = []
        dropped = 0
        max_exposure = max(1, int(settings.scanner.max_same_direction_exposure))
        max_in_run = max(1, int(settings.scanner.max_correlated_signals_per_run))

        for candidate, bundle in sorted(candidates, key=lambda item: item[0].score, reverse=True):
            key = (self._portfolio_bucket(candidate.exchange_symbol), candidate.direction)
            existing = existing_counts.get(key, 0)
            pending = in_run_counts.get(key, 0)
            reason = ""
            if existing + pending >= max_exposure:
                reason = "portfolio_risk:max_same_direction_exposure"
            elif pending >= max_in_run:
                reason = "portfolio_risk:max_correlated_signals_per_run"
            if reason:
                dropped += 1
                await self._audit(
                    run_id,
                    "portfolio_risk",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason=reason,
                    payload={
                        "bucket": key[0],
                        "direction": key[1],
                        "existing_exposure": existing,
                        "pending_this_run": pending,
                        "max_same_direction_exposure": max_exposure,
                        "max_correlated_signals_per_run": max_in_run,
                    },
                )
                continue
            in_run_counts[key] = pending + 1
            kept.append((candidate, bundle))
        return kept, dropped

    def _build_run_funnel(
        self,
        *,
        scanned: int,
        data_failures: int,
        filtered: int,
        direction_conflicts: int,
        candidates: int,
        selected: int,
        processed: list[dict[str, Any]],
        filter_reasons: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        ai_used = 0
        for item in processed:
            status = str(item.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            if item.get("ai_used"):
                ai_used += 1
        return {
            "scanned": scanned,
            "data_failures": data_failures,
            "filtered": filtered,
            "direction_conflicts": direction_conflicts,
            "candidates": candidates,
            "selected": selected,
            "processed": len(processed),
            "ai_used": ai_used,
            "statuses": statuses,
            "filter_reasons": dict(sorted((filter_reasons or {}).items(), key=lambda item: item[1], reverse=True)),
            "universe": self._last_status.get("last_universe") or {},
        }

    def _weighted(self, name: str, value: float) -> float:
        try:
            factor = float((settings.scanner.score_weights or {}).get(name, 1.0))
        except (TypeError, ValueError):
            factor = 1.0
        return value * factor

    def _threshold_key(self, symbol: str, timeframe: str, direction: str) -> list[str]:
        symbol_key = str(symbol or "*").upper().strip() or "*"
        tf_key = str(timeframe or "*").lower().strip() or "*"
        direction_key = str(direction or "*").lower().strip() or "*"
        return [
            f"{symbol_key}|{tf_key}|{direction_key}",
            f"{symbol_key}|{tf_key}|*",
            f"*|{tf_key}|{direction_key}",
            "*|*|*",
        ]

    def _min_score_for(self, symbol: str, timeframe: str, direction: str) -> float:
        base = float(self._adaptive_min_score or settings.scanner.min_score)
        if not settings.scanner.walk_forward_enabled:
            return base
        for key in self._threshold_key(symbol, timeframe, direction):
            item = self._threshold_overrides.get(key)
            if not item:
                continue
            threshold = _safe_float(item.get("threshold"), 0.0)
            if threshold > 0:
                return max(float(settings.scanner.adaptive_min_score_floor), min(float(settings.scanner.adaptive_min_score_ceiling), threshold))
        return base

    async def _refresh_learning(self, run_id: str) -> dict[str, Any]:
        if not settings.scanner.learning_enabled or db_manager.async_session_factory is None:
            self._threshold_overrides = {}
            return {"enabled": False}
        try:
            async with db_manager.async_session_factory() as session:
                sync_result = await sync_scanner_outcomes(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    days=settings.scanner.outcome_lookback_days,
                    max_positions=settings.scanner.outcome_max_sync_positions,
                )
                outcome_summary = await compute_outcome_summary(
                    session,
                    scope=self.scope,
                    days=settings.scanner.outcome_lookback_days,
                    include_recent=False,
                )
                threshold_summary = await compute_walk_forward_thresholds(
                    session,
                    scope=self.scope,
                    days=settings.scanner.outcome_lookback_days,
                ) if settings.scanner.walk_forward_enabled else {"thresholds": {}}
                self._threshold_overrides = dict(threshold_summary.get("thresholds") or {})
                await session.commit()
                return {
                    "enabled": True,
                    "synced_outcomes": int(sync_result.get("synced") or 0),
                    "outcome_labels": int(outcome_summary.get("total") or 0),
                    "win_rate": outcome_summary.get("win_rate"),
                    "expectancy_pct": outcome_summary.get("expectancy_pct"),
                    "walk_forward_thresholds": len(self._threshold_overrides),
                }
        except Exception as exc:
            logger.warning(f"[ScannerLearning] Refresh failed: {exc}")
            self._threshold_overrides = {}
            return {"enabled": True, "error": str(exc)}

    async def _fetch_bundle_with_retry(self, watch_symbol: str | ScannerUniverseItem) -> OHLCVBundle:
        item = self._coerce_universe_item(watch_symbol)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                try:
                    return await self.provider.get_bundle(
                        item.watch_symbol,
                        settings.scanner.timeframes,
                        **item.mapping_overrides(),
                    )
                except TypeError as exc:
                    if "unexpected" not in str(exc).lower() and "keyword" not in str(exc).lower():
                        raise
                    return await self.provider.get_bundle(item.watch_symbol, settings.scanner.timeframes)
            except Exception as exc:
                last_error = exc
                text = str(exc).lower()
                rate_limited = any(key in text for key in ("rate limit", "429", "too many requests"))
                delay = min(8.0, 2.0 ** attempt) if rate_limited else min(3.0, 0.5 * (attempt + 1))
                if attempt >= 2:
                    break
                logger.warning(
                    f"[Scanner] Data fetch retry {attempt + 1}/2 for {item.watch_symbol} "
                    f"in {delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)
        raise last_error or RuntimeError(f"Failed to fetch scanner bundle for {item.watch_symbol}")

    def _candidate_directions(self, indicators: dict[str, Any], current: float) -> list[str]:
        directions: list[str] = []
        rsi = _safe_float(indicators.get("rsi"), 50.0)
        ema_fast = _safe_float(indicators.get("ema_fast"))
        ema_slow = _safe_float(indicators.get("ema_slow"))
        atr_pct = _safe_float(indicators.get("atr_pct"))

        if atr_pct and atr_pct < float(settings.scanner.min_atr_pct):
            return []
        if rsi <= float(settings.scanner.rsi_lower):
            directions.append("long")
        if rsi >= float(settings.scanner.rsi_upper):
            directions.append("short")
        if ema_fast > 0 and ema_slow > 0:
            if ema_fast > ema_slow and current >= ema_fast:
                directions.append("long")
            elif ema_fast < ema_slow and current <= ema_fast:
                directions.append("short")
        return list(dict.fromkeys(directions))

    def _analyze_smc(self, bundle: OHLCVBundle, direction: str) -> dict[str, Any]:
        from smc_analyzer import analyze_smc_single_tf

        contexts: dict[str, Any] = {}
        atr_pct = _safe_float(bundle.indicators.get(bundle.primary_timeframe, {}).get("atr_pct"))
        for timeframe, candles in bundle.candles.items():
            rows = _ohlcv_rows(candles)
            if len(rows) < 11:
                continue
            try:
                contexts[timeframe] = analyze_smc_single_tf(
                    rows,
                    timeframe,
                    bundle.current_price,
                    direction,
                    atr_pct,
                )
            except Exception as exc:
                logger.debug(f"[Scanner] SMC failed for {bundle.mapping.watch_symbol} {timeframe}: {exc}")
        return contexts

    def _build_scoring_context(
        self,
        bundle: OHLCVBundle,
        ctx: Any,
        direction: str,
        primary_indicators: dict[str, Any],
        timeframe: str | None = None,
        htf_trend: str | None = None,
    ) -> ScoringContext:
        """Build ScoringContext from bundle and SMC context for rule engine."""
        current = float(bundle.current_price or 0.0)
        atr_pct = _safe_float(primary_indicators.get("atr_pct"))
        atr_price = max(current * max(atr_pct, 0.05) / 100.0, current * 0.001)
        rsi = _safe_float(primary_indicators.get("rsi"), 50.0)
        ema_fast = _safe_float(primary_indicators.get("ema_fast"))
        ema_slow = _safe_float(primary_indicators.get("ema_slow"))
        ema200 = _safe_float(primary_indicators.get("ema200"))
        macd_hist = _safe_float(primary_indicators.get("macd_hist"))
        adx = _safe_float(primary_indicators.get("adx"))
        volume_ratio_raw = primary_indicators.get("volume_ratio")
        volume_ratio = _safe_float(volume_ratio_raw)
        vwap = _safe_float(primary_indicators.get("vwap"))
        vwap_dist = _safe_float(primary_indicators.get("vwap_distance_pct"))
        poc = _safe_float(primary_indicators.get("volume_profile_poc"))
        regime = str(primary_indicators.get("market_regime") or "unknown").lower()
        oi_change = bundle.oi_change_pct

        support = self._best_support_zone(ctx, direction, current, atr_price)
        structure = getattr(ctx, "structure", None)
        smc_trend = str(getattr(structure, "trend", "ranging") or "ranging").lower()
        premium = _safe_float(getattr(ctx, "premium_zone", 0.0))
        discount = _safe_float(getattr(ctx, "discount_zone", 0.0))
        equilibrium = _safe_float(getattr(ctx, "equilibrium", 0.0))
        risk_score = _safe_float(getattr(ctx, "risk_score", 0.5), 0.5)
        timing_score = _safe_float(getattr(ctx, "entry_timing_score", 0.5), 0.5)

        if direction == "long":
            if discount and current <= discount:
                price_zone = "discount"
            elif equilibrium and current <= equilibrium:
                price_zone = "below_equilibrium"
            else:
                price_zone = "premium_or_neutral"
        else:
            if premium and current >= premium:
                price_zone = "premium"
            elif equilibrium and current >= equilibrium:
                price_zone = "above_equilibrium"
            else:
                price_zone = "discount_or_neutral"

        return ScoringContext(
            direction=direction,
            current_price=current,
            atr_pct=atr_pct,
            atr_price=atr_price,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema200=ema200,
            macd_hist=macd_hist,
            adx=adx,
            volume_ratio=volume_ratio,
            vwap=vwap,
            vwap_distance_pct=vwap_dist,
            poc=poc,
            regime=regime,
            oi_change_pct=oi_change,
            htf_trend=htf_trend,
            smc_trend=smc_trend,
            smc_risk_score=risk_score,
            smc_timing_score=timing_score,
            price_zone=price_zone,
            support_zone=support,
            premium_zone=premium,
            discount_zone=discount,
            equilibrium=equilibrium,
            spread_pct=bundle.bid_ask_spread_pct,
            bid_ask_spread_pct=bundle.bid_ask_spread_pct,
            bundle_quality_passed=bundle.quality_passed,
            bundle_quality_reasons=list(bundle.quality_reasons or []),
            timeframe=str(timeframe or bundle.primary_timeframe),
            market_type=bundle.mapping.market_type or settings.exchange.market_type,
        )

    def _estimate_risk_reward(self, ctx: ScoringContext) -> float:
        support = ctx.support_zone or {}
        current = float(ctx.current_price or 0.0)
        atr = max(float(ctx.atr_price or 0.0), current * 0.001)
        if current <= 0 or not support:
            return 0.0
        low = _safe_float(support.get("low"))
        high = _safe_float(support.get("high"))
        if low <= 0 or high <= 0:
            return 0.0

        if ctx.direction == "short":
            stop = max(low, high) + atr * 0.25
            targets = [value for value in (ctx.equilibrium, ctx.discount_zone) if value > 0 and value < current]
            if not targets or stop <= current:
                return 0.0
            target = max(targets)
            risk = stop - current
            reward = current - target
        else:
            stop = min(low, high) - atr * 0.25
            targets = [value for value in (ctx.equilibrium, ctx.premium_zone) if value > 0 and value > current]
            if not targets or stop >= current:
                return 0.0
            target = min(targets)
            risk = current - stop
            reward = target - current
        if risk <= 0 or reward <= 0:
            return 0.0
        return round(reward / risk, 4)

    def _minutes_to_next_funding_boundary(self) -> int:
        now = utcnow()
        current_minutes = now.hour * 60 + now.minute
        boundaries = [0, 8 * 60, 16 * 60, 24 * 60]
        distances = [abs(current_minutes - item) for item in boundaries]
        distances.append(abs((24 * 60 + current_minutes) - boundaries[-2]))
        return int(min(distances))

    def _in_utc_window(self, window: str) -> bool:
        text = str(window or "").strip().lower()
        if "-" not in text:
            return False
        start_text, _, end_text = text.partition("-")

        def minutes(value: str) -> int | None:
            try:
                hour_text, _, minute_text = value.strip().partition(":")
                hour = int(hour_text)
                minute = int(minute_text or "0")
            except ValueError:
                return None
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            return hour * 60 + minute

        start = minutes(start_text)
        end = minutes(end_text)
        if start is None or end is None:
            return False
        now = utcnow()
        current = now.hour * 60 + now.minute
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def _event_session_filter(self, bundle: OHLCVBundle) -> tuple[bool, str, dict[str, Any]]:
        diagnostics = {
            "enabled": bool(settings.scanner.event_filter_enabled),
            "funding_rate": bundle.funding_rate,
            "utc_hour": utcnow().hour,
            "low_liquidity_utc_hours": list(settings.scanner.low_liquidity_utc_hours or []),
            "event_blackout_utc_windows": list(settings.scanner.event_blackout_utc_windows or []),
        }
        if not settings.scanner.event_filter_enabled:
            return True, "", diagnostics
        if utcnow().hour in {int(item) for item in settings.scanner.low_liquidity_utc_hours or []}:
            return False, "event_filter:low_liquidity_utc_hour", diagnostics
        for window in settings.scanner.event_blackout_utc_windows or []:
            if self._in_utc_window(str(window)):
                diagnostics["matched_window"] = window
                return False, "event_filter:configured_blackout_window", diagnostics
        funding = bundle.funding_rate
        if funding is not None and abs(float(funding)) > float(settings.scanner.max_abs_funding_rate):
            return False, "event_filter:funding_rate_extreme", diagnostics
        blackout = int(settings.scanner.funding_blackout_minutes)
        if (
            blackout > 0
            and bundle.funding_rate is not None
            and str(bundle.mapping.market_type or settings.exchange.market_type).lower() in {"contract", "swap", "future"}
        ):
            minutes = self._minutes_to_next_funding_boundary()
            diagnostics["minutes_to_funding_boundary"] = minutes
            if minutes <= blackout:
                return False, "event_filter:funding_settlement_blackout", diagnostics
        return True, "", diagnostics

    def _estimated_liquidity_slippage_pct(self, side_depth: float, order_size: float, volume_24h: float, spread_pct: float) -> float:
        if order_size <= 0:
            return spread_pct
        depth_component = (order_size / max(side_depth, 1.0)) * 100.0 if side_depth > 0 else 0.0
        volume_component = (order_size / max(volume_24h, 1.0)) * 100.0 if volume_24h > 0 else 0.0
        return round(max(0.0, spread_pct + depth_component * 0.10 + volume_component * 5.0), 4)

    def _liquidity_filter(self, bundle: OHLCVBundle, direction: str) -> tuple[bool, str, dict[str, Any]]:
        dq = bundle.data_quality or {}
        volume_24h = _safe_float(bundle.volume_24h)
        bid_depth = _safe_float(dq.get("orderbook_bid_depth_usdt"))
        ask_depth = _safe_float(dq.get("orderbook_ask_depth_usdt"))
        side_depth = ask_depth if direction == "long" else bid_depth
        order_size = float(settings.scanner.liquidity_order_size_usdt)
        spread = bundle.bid_ask_spread_pct
        slippage = self._estimated_liquidity_slippage_pct(side_depth, order_size, volume_24h, spread)
        imbalance = bundle.orderbook_imbalance
        diagnostics = {
            "enabled": bool(settings.scanner.liquidity_filter_enabled),
            "direction": direction,
            "order_size_usdt": order_size,
            "volume_24h": volume_24h,
            "bid_depth_usdt": bid_depth,
            "ask_depth_usdt": ask_depth,
            "side_depth_usdt": side_depth,
            "spread_pct": spread,
            "estimated_slippage_pct": slippage,
            "orderbook_imbalance": imbalance,
        }
        if not settings.scanner.liquidity_filter_enabled:
            return True, "", diagnostics
        if volume_24h > 0 and volume_24h < float(settings.scanner.min_quote_volume_24h):
            return False, "liquidity_filter:quote_volume_too_low", diagnostics
        if side_depth > 0 and side_depth < float(settings.scanner.min_orderbook_depth_usdt):
            return False, "liquidity_filter:orderbook_depth_too_low", diagnostics
        if slippage > float(settings.scanner.max_estimated_slippage_pct):
            return False, "liquidity_filter:estimated_slippage_too_high", diagnostics
        if imbalance is not None:
            if direction == "long" and float(imbalance) < float(settings.scanner.min_orderbook_imbalance_long):
                return False, "liquidity_filter:orderbook_against_long", diagnostics
            if direction == "short" and float(imbalance) > float(settings.scanner.max_orderbook_imbalance_short):
                return False, "liquidity_filter:orderbook_against_short", diagnostics
        return True, "", diagnostics

    def _hard_filter_scoring_context(
        self,
        bundle: OHLCVBundle,
        ctx: ScoringContext,
        primary_indicators: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        rr = self._estimate_risk_reward(ctx)
        liquidity_ok, liquidity_reason, liquidity = self._liquidity_filter(bundle, ctx.direction)
        diagnostics = {
            "enabled": bool(settings.scanner.hard_filters_enabled),
            "estimated_rr": rr,
            "liquidity": liquidity,
            "support_zone": bool(ctx.support_zone),
            "smc_trend": ctx.smc_trend,
            "htf_trend": ctx.htf_trend or "",
        }
        if not settings.scanner.hard_filters_enabled:
            return True, "", diagnostics
        if settings.scanner.require_support_zone and not ctx.support_zone:
            return False, "hard_filter:no_support_zone", diagnostics
        if settings.scanner.require_structure_alignment and ctx.smc_trend not in {ctx.expected_trend, "ranging"}:
            return False, f"hard_filter:smc_structure_{ctx.smc_trend}_vs_{ctx.direction}", diagnostics
        if ctx.htf_conflicts:
            return False, f"hard_filter:htf_conflict_{ctx.htf_trend}", diagnostics
        if ctx.atr_pct > 0 and ctx.atr_pct < float(settings.scanner.min_atr_pct):
            return False, "hard_filter:atr_too_low", diagnostics
        if ctx.spread_pct > float(settings.scanner.max_spread_pct):
            return False, "hard_filter:spread_too_wide", diagnostics
        if primary_indicators.get("volume_ratio") is not None and ctx.volume_ratio < float(settings.scanner.min_volume_ratio):
            return False, "hard_filter:volume_too_low", diagnostics
        if float(settings.scanner.min_rr_ratio) > 0 and rr < float(settings.scanner.min_rr_ratio):
            return False, "hard_filter:risk_reward_too_low", diagnostics
        if not liquidity_ok:
            return False, liquidity_reason, diagnostics
        return True, "", diagnostics

    def _score_smc_candidate(
        self,
        bundle: OHLCVBundle,
        ctx: Any,
        timeframe: str,
        direction: str,
        primary_indicators: dict[str, Any],
        htf_trend: str | None = None,
    ) -> ScannerCandidate | None:
        mapping = bundle.mapping
        current = float(bundle.current_price or 0.0)

        scoring_ctx = self._build_scoring_context(
            bundle,
            ctx,
            direction,
            primary_indicators,
            timeframe=timeframe,
            htf_trend=htf_trend,
        )

        hard_ok, hard_reason, hard_diagnostics = self._hard_filter_scoring_context(bundle, scoring_ctx, primary_indicators)
        if not hard_ok:
            logger.debug(
                f"[Scanner] Hard filter rejected {mapping.exchange_symbol} {timeframe} "
                f"{direction}: {hard_reason}"
            )
            return None

        score, reasons, breakdown = DEFAULT_ENGINE.evaluate(scoring_ctx)

        liquidity_tier = mapping.liquidity_tier or self._liquidity_tier(mapping.exchange_symbol, float(bundle.volume_24h or 0.0))
        tier_threshold_bump = self._liquidity_tier_threshold_bump(liquidity_tier, mapping.universe_source)
        threshold = self._min_score_for(mapping.exchange_symbol, timeframe, direction) + tier_threshold_bump
        fusion_floor = max(0.0, threshold - max(12.0, float(settings.scanner.mtf_confirmation_bonus) * 2.0))
        if score < fusion_floor:
            return None

        support = scoring_ctx.support_zone
        support_mid = _safe_float((support or {}).get("midpoint"), current)
        setup_type = str((support or {}).get("type") or "indicator_smc")
        price_zone_key = f"{scoring_ctx.price_zone}:{round(support_mid, 2)}"
        setup_hash = _setup_hash(
            ticker=mapping.exchange_symbol,
            direction=direction,
            timeframe=timeframe,
            setup_type=setup_type,
            price_zone=price_zone_key,
            reference_price=support_mid,
        )

        smc_summary = {
            "timeframe": timeframe,
            "trend": scoring_ctx.smc_trend,
            "risk_score": round(scoring_ctx.smc_risk_score, 4),
            "entry_timing_score": round(scoring_ctx.smc_timing_score, 4),
            "timing_recommendation": getattr(ctx, "timing_recommendation", ""),
            "premium_zone": scoring_ctx.premium_zone,
            "discount_zone": scoring_ctx.discount_zone,
            "equilibrium": scoring_ctx.equilibrium,
            "zone": scoring_ctx.price_zone,
            "support_type": setup_type,
            "support_midpoint": support_mid,
            "support": support or {},
        }

        return ScannerCandidate(
            watch_symbol=mapping.watch_symbol,
            exchange_symbol=mapping.exchange_symbol,
            exchange_name=mapping.exchange_name,
            market_type=mapping.market_type or settings.exchange.market_type,
            data_source=mapping.data_source,
            mapped_asset=mapping.mapped_asset,
            direction=direction,
            timeframe=timeframe,
            current_price=current,
            entry_reference=support_mid,
            score=score,
            setup_type=setup_type,
            price_zone=price_zone_key,
            setup_hash=setup_hash,
            reasons=reasons,
            indicator_summary={
                "rsi": scoring_ctx.rsi,
                "atr_pct": scoring_ctx.atr_pct,
                "ema_fast": scoring_ctx.ema_fast,
                "ema_slow": scoring_ctx.ema_slow,
                "ema200": scoring_ctx.ema200,
                "macd_hist": scoring_ctx.macd_hist,
                "adx": scoring_ctx.adx,
                "volume_ratio": scoring_ctx.volume_ratio,
                "spread_pct": scoring_ctx.spread_pct,
                "vwap": scoring_ctx.vwap,
                "vwap_distance_pct": scoring_ctx.vwap_distance_pct,
                "poc": scoring_ctx.poc,
                "regime": scoring_ctx.regime,
                "oi_change_pct": scoring_ctx.oi_change_pct,
                "htf_trend": htf_trend or "",
            },
            smc_summary=smc_summary,
            quality={
                "reasons": list(bundle.quality_reasons or []),
                "passed": bundle.quality_passed,
                "hard_filters": hard_diagnostics,
                "min_score_threshold": threshold,
                "data_source_policy": mapping.data_source_policy,
                "target_exchange": mapping.target_exchange or mapping.exchange_name,
                "source_exchange": mapping.source_exchange,
                "actual_data_source": mapping.actual_data_source or bundle.data_quality.get("actual_data_source"),
                "tradable": mapping.tradable,
                "tradability_reason": mapping.tradability_reason,
                "universe_source": mapping.universe_source,
                "liquidity_tier": liquidity_tier,
                "liquidity_tier_threshold_bump": tier_threshold_bump,
            },
            score_breakdown=breakdown,
            target_exchange=mapping.target_exchange or mapping.exchange_name or settings.exchange.name,
            target_market_type=mapping.target_market_type or mapping.market_type or settings.exchange.market_type,
            source_exchange=mapping.source_exchange or "",
            source_market_type=mapping.source_market_type or "",
            actual_data_source=mapping.actual_data_source or bundle.data_quality.get("actual_data_source", ""),
            data_source_policy=mapping.data_source_policy,
            tradable=mapping.tradable,
            tradability_reason=mapping.tradability_reason,
            universe_source=mapping.universe_source,
            liquidity_tier=liquidity_tier,
        )

    def _best_support_zone(self, ctx: Any, direction: str, current: float, atr_price: float) -> dict[str, Any] | None:
        desired_type = "bullish" if direction == "long" else "bearish"
        matches: list[dict[str, Any]] = []

        for fvg in getattr(ctx, "fvgs", []) or []:
            if str(getattr(fvg, "type", "")).lower() != desired_type:
                continue
            if bool(getattr(fvg, "filled", False)):
                continue
            low = _safe_float(getattr(fvg, "bottom", 0.0))
            high = _safe_float(getattr(fvg, "top", 0.0))
            midpoint = _safe_float(getattr(fvg, "midpoint", 0.0))
            distance = _zone_distance(current, low, high)
            if distance <= atr_price * 1.5:
                matches.append({
                    "type": f"{desired_type}_fvg",
                    "low": min(low, high),
                    "high": max(low, high),
                    "midpoint": midpoint,
                    "distance": distance,
                    "effectiveness": _safe_float(getattr(fvg, "effectiveness", 1.0), 1.0),
                })

        for ob in getattr(ctx, "order_blocks", []) or []:
            if str(getattr(ob, "type", "")).lower() != desired_type:
                continue
            status = str(getattr(ob, "mitigation_status", "") or "").lower()
            if status in {"mitigated", "broken"}:
                continue
            low = _safe_float(getattr(ob, "low", 0.0))
            high = _safe_float(getattr(ob, "high", 0.0))
            midpoint = _safe_float(getattr(ob, "midpoint", 0.0))
            distance = _zone_distance(current, low, high)
            if distance <= atr_price * 1.5:
                matches.append({
                    "type": f"{desired_type}_order_block",
                    "low": min(low, high),
                    "high": max(low, high),
                    "midpoint": midpoint,
                    "distance": distance,
                    "strength": _safe_float(getattr(ob, "strength", 0.0)),
                    "effectiveness": _safe_float(getattr(ob, "effectiveness", 1.0), 1.0),
                    "mitigation_status": status or "unknown",
                })

        if not matches:
            return None
        matches.sort(key=lambda item: (item["distance"], -_safe_float(item.get("effectiveness"), 1.0)))
        return matches[0]

    async def _check_existing_position(
        self, exchange_symbol: str, direction: str
    ) -> tuple[bool, str]:
        """Check if an open or pending position already exists for the same symbol+direction.

        Returns (has_conflict, reason).
        - Same-direction open/pending position → conflict (skip signal).
        - Opposite-direction open/pending position → no conflict (market reversal, allow signal).
        - No position → no conflict (allow signal).
        """
        try:
            target_key = position_symbol_key(exchange_symbol)
            if not target_key:
                return False, ""
            async with db_manager.async_session_factory() as session:
                stmt = select(PositionModel).where(
                    PositionModel.status.in_(["open", "pending"]),
                )
                result = await session.execute(stmt)
                positions = result.scalars().all()
            for pos in positions:
                if position_symbol_key(pos.ticker) != target_key:
                    continue
                pos_dir = (pos.direction or "").lower()
                if pos_dir == direction.lower():
                    return True, (
                        f"Existing {pos_dir} position/order on {pos.ticker} "
                        f"(status={pos.status}, id={pos.id[:8]})"
                    )
            return False, ""
        except Exception as exc:
            logger.warning(f"[Scanner] Position conflict check failed (allowing signal): {exc}")
            return False, ""

    async def _dispatch_candidate(
        self, run_id: str, candidate: ScannerCandidate, bundle: OHLCVBundle
    ) -> dict[str, Any]:
        async with self._dispatch_lock:
            return await self._dispatch_candidate_locked(run_id, candidate, bundle)

    async def _dispatch_candidate_locked(
        self, run_id: str, candidate: ScannerCandidate, bundle: OHLCVBundle
    ) -> dict[str, Any]:
        async with db_manager.async_session_factory() as session:
            state = await get_or_create_scanner_state(session, scope=self.scope)
            if (
                settings.scanner.max_signals_per_day
                and int(state.signal_count or 0) >= settings.scanner.max_signals_per_day
            ):
                await record_scanner_audit(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    event_type="daily_limit",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason="daily scanner signal limit reached",
                )
                await session.commit()
                return {"status": "skipped", "reason": "daily signal limit reached", "setup_hash": candidate.setup_hash}
            if (
                settings.scanner.max_ai_calls_per_day
                and int(state.ai_call_count or 0) >= settings.scanner.max_ai_calls_per_day
            ):
                await update_scanner_state_counts(
                    session,
                    self.scope,
                    degraded_mode="ai_budget_exhausted",
                    degraded_reason="daily scanner AI call limit reached",
                )
                await record_scanner_audit(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    event_type="ai_budget_exhausted",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason="daily AI call limit reached",
                )
                await session.commit()
                return {
                    "status": "skipped",
                    "reason": "daily AI call limit reached",
                    "setup_hash": candidate.setup_hash,
                }

            market_ok, market_reason, market_limits = await self._validate_live_market(candidate)
            if not market_ok:
                await record_scanner_audit(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    event_type="live_market_invalid",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason=market_reason,
                    payload={
                        "exchange": candidate.exchange_name,
                        "market_type": candidate.market_type,
                        "target_exchange": candidate.target_exchange or candidate.exchange_name,
                        "target_market_type": candidate.target_market_type or candidate.market_type,
                        "source_exchange": candidate.source_exchange,
                        "actual_data_source": candidate.actual_data_source,
                        "limits": market_limits,
                    },
                )
                await session.commit()
                return {"status": "skipped", "reason": market_reason, "setup_hash": candidate.setup_hash}

            has_conflict, conflict_reason = await self._check_existing_position(
                candidate.exchange_symbol, candidate.direction
            )
            if has_conflict:
                await record_scanner_audit(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    event_type="position_conflict",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason=f"duplicate signal blocked: {conflict_reason}",
                )
                await session.commit()
                return {
                    "status": "skipped",
                    "reason": f"position conflict: {conflict_reason}",
                    "setup_hash": candidate.setup_hash,
                }

            acquired, lock = await acquire_scanner_setup_lock(
                session,
                scope=self.scope,
                setup_hash=candidate.setup_hash,
                watch_symbol=candidate.watch_symbol,
                exchange_symbol=candidate.exchange_symbol,
                direction=candidate.direction,
                timeframe=candidate.timeframe,
                setup_type=candidate.setup_type,
                price_zone=candidate.price_zone,
                ttl_seconds=settings.scanner.setup_cooldown_secs,
            )
            if not acquired:
                await record_scanner_audit(
                    session,
                    scope=self.scope,
                    run_id=run_id,
                    event_type="deduped",
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    direction=candidate.direction,
                    score=candidate.score,
                    setup_hash=candidate.setup_hash,
                    reason="setup cooldown active",
                    payload={"expires_at": getattr(lock, "expires_at", None)},
                )
                await session.commit()
                return {"status": "skipped", "reason": "setup cooldown active", "setup_hash": candidate.setup_hash}

            await record_scanner_audit(
                session,
                scope=self.scope,
                run_id=run_id,
                event_type="sent_to_ai",
                watch_symbol=candidate.watch_symbol,
                exchange_symbol=candidate.exchange_symbol,
                direction=candidate.direction,
                score=candidate.score,
                setup_hash=candidate.setup_hash,
                reason="synthetic signal dispatched to processor",
                payload=candidate.to_payload(),
            )
            await session.commit()

            signal, raw_body = build_synthetic_signal(candidate)
            market = market_context_from_bundle(bundle, ticker=candidate.exchange_symbol)
            processor = SignalProcessor(session)
            result = await processor.process_scanner_signal(
                signal,
                scanner_mode=settings.scanner.mode,
                scanner_payload=raw_body,
                market=market,
            )

            ai_used = isinstance(result.get("analysis"), dict)
            if ai_used:
                await update_scanner_state_counts(session, self.scope, ai_call_delta=1, signal_delta=1)

            cooldown_level = await self._get_symbol_cooldown_level(candidate.exchange_symbol)
            cooldown_ttl = self._symbol_cooldown_ttl_for_result(result, candidate.score, cooldown_level)
            if cooldown_ttl > 0:
                await set_scanner_symbol_cooldown(
                    session,
                    scope=self.scope,
                    watch_symbol=candidate.watch_symbol,
                    exchange_symbol=candidate.exchange_symbol,
                    ttl_seconds=cooldown_ttl,
                )
            await record_scanner_audit(
                session,
                scope=self.scope,
                run_id=run_id,
                event_type="result",
                watch_symbol=candidate.watch_symbol,
                exchange_symbol=candidate.exchange_symbol,
                direction=candidate.direction,
                score=candidate.score,
                setup_hash=candidate.setup_hash,
                reason=str(result.get("reason", "")),
                payload={
                    "result": result,
                    "ai_used": ai_used,
                    "cooldown_ttl_secs": cooldown_ttl,
                    "target_exchange": candidate.target_exchange or candidate.exchange_name,
                    "source_exchange": candidate.source_exchange,
                    "actual_data_source": candidate.actual_data_source,
                    "tradable": candidate.tradable,
                    "tradability_reason": candidate.tradability_reason,
                },
            )
            await self._update_win_rate(
                session,
                str(result.get("status", "")),
                str(result.get("reason", "")),
                would_execute=bool(result.get("would_execute")),
            )
            await session.commit()
            return {
                "status": result.get("status", "unknown"),
                "reason": result.get("reason", ""),
                "symbol": candidate.exchange_symbol,
                "direction": candidate.direction,
                "score": candidate.score,
                "setup_hash": candidate.setup_hash,
                "ai_used": ai_used,
                "cooldown_ttl_secs": cooldown_ttl,
                "target_exchange": candidate.target_exchange or candidate.exchange_name,
                "source_exchange": candidate.source_exchange,
                "actual_data_source": candidate.actual_data_source,
                "tradable": candidate.tradable,
            }

    def _symbol_cooldown_ttl_for_result(self, result: dict[str, Any], candidate_score: float = 0.0, cooldown_level: int = 0) -> int:
        """Compute symbol cooldown TTL with progressive scaling.

        Args:
            result: Processing result dict
            candidate_score: Score of the candidate (affects cooldown duration)
            cooldown_level: Current cooldown level (progressive multiplier)

        Returns:
            Cooldown TTL in seconds
        """
        status = str(result.get("status") or "").lower().strip()
        reason = str(result.get("reason") or "").lower()
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        recommendation = str(analysis.get("recommendation") or "").lower().strip()

        base_cooldown = max(0, int(settings.scanner.symbol_cooldown_secs))
        rejected_cooldown = max(0, int(settings.scanner.rejected_symbol_cooldown_secs))
        blocked_cooldown = max(0, int(settings.scanner.blocked_symbol_cooldown_secs))

        if settings.scanner.adaptive_threshold_enabled:
            max_levels = int(settings.scanner.adaptive_cooldown_levels)
            multiplier = float(settings.scanner.adaptive_cooldown_multiplier)
            base_cooldown = int(settings.scanner.adaptive_cooldown_base_secs)

            level_penalty = min(cooldown_level, max_levels)
            progressive_multiplier = multiplier ** level_penalty

            score_bonus = max(0.0, (candidate_score - 80.0) / 20.0)
            score_reduction = 1.0 - min(0.5, score_bonus * 0.25)

            base_cooldown = int(base_cooldown * progressive_multiplier * score_reduction)

        if status in {"duplicate", "blocked", "error"}:
            return blocked_cooldown if blocked_cooldown > 0 else base_cooldown * 3
        if status == "rejected" or recommendation in {"reject", "hold"} or "rejected" in reason:
            ttl = rejected_cooldown if rejected_cooldown > 0 else base_cooldown * 2
            if cooldown_level > 0:
                ttl = int(ttl * (1.5 ** min(cooldown_level, 5)))
            return ttl
        if status == "observed" and not bool(result.get("would_execute")):
            ttl = rejected_cooldown if rejected_cooldown > 0 else base_cooldown * 2
            return ttl

        if status in {"executed", "paper_executed"}:
            if candidate_score >= 85.0:
                return max(60, int(base_cooldown * 0.5))

        return base_cooldown

    async def _get_symbol_cooldown_level(self, exchange_symbol: str) -> int:
        """Get current cooldown level for a symbol from recent audit history."""
        try:
            async with db_manager.async_session_factory() as session:
                stmt = select(ScannerAuditModel).where(
                    ScannerAuditModel.exchange_symbol == exchange_symbol,
                    ScannerAuditModel.event_type == "result",
                ).order_by(desc(ScannerAuditModel.created_at)).limit(10)
                result = await session.execute(stmt)
                recent = result.scalars().all()

                level = 0
                for audit in recent:
                    payload = json.loads(audit.payload_json or "{}")
                    res_status = str(payload.get("result", {}).get("status", "") or "").lower()
                    if res_status in {"rejected", "blocked", "error"}:
                        level += 1
                    elif res_status in {"executed", "paper_executed"}:
                        level = max(0, level - 1)
                return level
        except Exception:
            return 0

    async def _validate_live_market(self, candidate: ScannerCandidate) -> tuple[bool, str, dict[str, Any]]:
        """Fail closed before AI calls if a live scanner symbol is not tradable."""
        if str(settings.scanner.mode).lower().strip() != "live":
            return True, "", {}
        if not candidate.tradable:
            return False, candidate.tradability_reason or "Scanner live mode blocked: symbol is not tradable", {}
        if not settings.exchange.live_trading:
            return False, "Scanner live mode requires global LIVE_TRADING=true", {}
        snapshot_symbols = {str(item).upper().strip() for item in self._live_universe_snapshot.get("symbols") or []}
        if snapshot_symbols and candidate.exchange_symbol.upper().strip() not in snapshot_symbols:
            return False, "Scanner live mode blocked: symbol is outside the current live universe snapshot", {}
        whitelist = {str(item).upper().strip() for item in settings.scanner.live_symbol_whitelist}
        if whitelist and candidate.exchange_symbol.upper().strip() not in whitelist:
            return False, "Scanner live mode blocked: symbol is not in SCANNER_LIVE_SYMBOL_WHITELIST", {}
        exchange_name = str(candidate.target_exchange or candidate.exchange_name or settings.exchange.name).lower().strip()
        market_type = str(candidate.target_market_type or candidate.market_type or settings.exchange.market_type).lower().strip()
        try:
            from exchange import get_market_limits

            limits = await asyncio.to_thread(get_market_limits, exchange_name, candidate.exchange_symbol, market_type)
        except Exception as exc:
            reason = f"Scanner live market pre-check failed for {candidate.exchange_symbol}: {exc}"
            logger.warning(f"[Scanner] {reason}")
            return False, reason, {}

        if not limits or not limits.get("symbol"):
            reason = (
                f"Scanner live mode blocked: {candidate.exchange_symbol} is not available "
                f"on {exchange_name}/{market_type}"
            )
            return False, reason, limits or {}
        return True, "", limits

    async def _symbol_cooldown(self, exchange_symbol: str) -> Any | None:
        async with db_manager.async_session_factory() as session:
            return await scanner_symbol_on_cooldown(
                session,
                scope=self.scope,
                exchange_symbol=exchange_symbol,
            )

    async def _audit(
        self,
        run_id: str,
        event_type: str,
        *,
        watch_symbol: str = "",
        exchange_symbol: str = "",
        direction: str = "",
        score: float = 0.0,
        setup_hash: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._audit_buffer_lock:
            self._audit_buffer.append({
                "scope": self.scope,
                "run_id": run_id,
                "event_type": event_type,
                "watch_symbol": watch_symbol,
                "exchange_symbol": exchange_symbol,
                "direction": direction,
                "score": score,
                "setup_hash": setup_hash,
                "reason": reason,
                "payload": payload,
            })

    async def _flush_audit_buffer(self) -> None:
        async with self._audit_buffer_lock:
            if not self._audit_buffer:
                return
            buffer = self._audit_buffer[:]
            self._audit_buffer.clear()
        async with db_manager.async_session_factory() as session:
            for item in buffer:
                await record_scanner_audit(session, **item)
            await session.commit()

    async def _update_win_rate(
        self,
        session: Any,
        result_status: str,
        result_reason: str,
        would_execute: bool = False,
    ) -> None:
        """Update win rate tracking after signal processing."""
        if not settings.scanner.adaptive_threshold_enabled:
            return
        if settings.scanner.learning_enabled:
            return
        state = await get_or_create_scanner_state(session, scope=self.scope)
        wins = int(state.signal_wins or 0)
        losses = int(state.signal_losses or 0)

        is_win = result_status in {"executed", "paper_executed", "observed_would_execute"} or (
            result_status == "observed" and would_execute
        )
        is_loss = result_status in {"rejected", "blocked", "error"} or "rejected" in result_reason.lower()

        if is_win:
            wins += 1
        elif is_loss:
            losses += 1

        total = wins + losses
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        state.signal_wins = wins
        state.signal_losses = losses
        state.signal_win_rate = round(win_rate, 2)
        state.last_win_rate_update_at = utcnow()

        try:
            history = json.loads(state.win_rate_history_json or "[]")
        except (json.JSONDecodeError, TypeError):
            history = []

        history.append({
            "timestamp": utcnow().isoformat(),
            "win_rate": round(win_rate, 2),
            "wins": wins,
            "losses": losses,
            "result_status": result_status,
        })
        history = history[-100:]
        state.win_rate_history_json = json.dumps(history)

        adaptive_score = self._compute_adaptive_min_score(win_rate, total)
        state.adaptive_min_score = round(adaptive_score, 2)

    def _compute_adaptive_min_score(self, win_rate: float, total_signals: int) -> float:
        """Compute adaptive min_score based on recent win rate."""
        if not settings.scanner.adaptive_threshold_enabled:
            return float(settings.scanner.min_score)

        if total_signals < 5:
            return float(settings.scanner.min_score)

        floor = float(settings.scanner.adaptive_min_score_floor)
        ceiling = float(settings.scanner.adaptive_min_score_ceiling)
        target = float(settings.scanner.adaptive_win_rate_target)
        step = float(settings.scanner.adaptive_adjustment_step)
        base_min = float(settings.scanner.min_score)

        deviation = win_rate - target

        adjustment = 0.0
        if deviation > 10.0:
            adjustment = -step * 2
        elif deviation > 5.0:
            adjustment = -step
        elif deviation < -10.0:
            adjustment = step * 2
        elif deviation < -5.0:
            adjustment = step

        adaptive = base_min + adjustment
        return max(floor, min(ceiling, adaptive))

    async def _get_effective_min_score(self) -> float:
        """Get effective min_score (static or adaptive from DB)."""
        if not settings.scanner.adaptive_threshold_enabled:
            return float(settings.scanner.min_score)
        async with db_manager.async_session_factory() as session:
            if settings.scanner.learning_enabled:
                summary = await compute_outcome_summary(
                    session,
                    scope=self.scope,
                    days=settings.scanner.outcome_lookback_days,
                    include_recent=False,
                )
                total = int(summary.get("total") or 0)
                if total >= max(5, int(settings.scanner.walk_forward_min_samples)):
                    return self._compute_adaptive_min_score(float(summary.get("win_rate") or 0.0), total)
            state = await get_or_create_scanner_state(session, scope=self.scope)
            adaptive = float(state.adaptive_min_score or 0.0)
            if adaptive > 0:
                return adaptive
            return float(settings.scanner.min_score)

    @staticmethod
    def _is_transient_failure(exc: Exception) -> bool:
        """Check if an exception is a transient failure that should be retried."""
        text = str(exc).lower()
        transient_keywords = ("rate limit", "429", "too many requests", "timeout", "connection error", "temporary")
        return any(keyword in text for keyword in transient_keywords)


_SCANNER_SERVICE: MarketScannerService | None = None


def get_market_scanner_service() -> MarketScannerService:
    global _SCANNER_SERVICE
    if _SCANNER_SERVICE is None:
        _SCANNER_SERVICE = MarketScannerService()
    return _SCANNER_SERVICE


async def run_scanner_once() -> dict[str, Any]:
    return await get_market_scanner_service().scan_once()


async def shutdown_market_scanner_service() -> None:
    if _SCANNER_SERVICE is not None:
        await _SCANNER_SERVICE.shutdown()

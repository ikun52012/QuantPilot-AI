"""Automatic market scanner service."""
from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select

from core.config import settings
from core.database import (
    PositionModel,
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
from services.signal_processor import SignalProcessor
from services.synthetic_signal import build_synthetic_signal, market_context_from_bundle
from services.unified_ohlcv import OHLCVBundle, UnifiedOHLCVProvider, timeframe_to_seconds


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
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class MarketScannerService:
    """Scans the configured watchlist and injects high-quality candidates."""

    def __init__(self, provider: UnifiedOHLCVProvider | None = None, scope: str = "admin") -> None:
        self.provider = provider or UnifiedOHLCVProvider()
        self.scope = scope
        self._scan_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._last_status: dict[str, Any] = {
            "running": False,
            "last_run_id": "",
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": "",
            "last_summary": {},
        }

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
        if not settings.scanner.watchlist:
            return {"status": "skipped", "reason": "SCANNER_WATCHLIST is empty"}
        if db_manager.async_session_factory is None:
            return {"status": "error", "reason": "Database not initialized (session_factory is None)"}
        if self._scan_lock.locked():
            return {"status": "skipped", "reason": "Scanner already running"}

        async with self._scan_lock:
            run_id = uuid.uuid4().hex[:12]
            started_at = utcnow()
            self._last_status.update({
                "running": True,
                "last_run_id": run_id,
                "last_started_at": started_at,
                "last_error": "",
            })
            try:
                return await self._scan_once_locked(run_id)
            except Exception as exc:
                self._last_status["last_error"] = str(exc)
                logger.exception(f"[Scanner] Run {run_id} failed: {exc}")
                async with db_manager.async_session_factory() as session:
                    await update_scanner_state_counts(
                        session,
                        self.scope,
                        degraded_mode="error",
                        degraded_reason=str(exc),
                    )
                    await record_scanner_audit(
                        session,
                        scope=self.scope,
                        run_id=run_id,
                        event_type="error",
                        reason=str(exc),
                    )
                    await session.commit()
                return {"status": "error", "reason": str(exc), "run_id": run_id}
            finally:
                self._last_status.update({
                    "running": False,
                    "last_finished_at": utcnow(),
                })

    async def _scan_once_locked(self, run_id: str) -> dict[str, Any]:
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
        for item in scan_results:
            scanned += int(item.get("scanned") or 0)
            data_failures += int(item.get("data_failures") or 0)
            filtered += int(item.get("filtered") or 0)
            candidates.extend(item.get("candidates") or [])

        candidates, direction_conflicts = await self._resolve_direction_conflicts(run_id, candidates)
        filtered += direction_conflicts
        candidates.sort(key=lambda item: item[0].score, reverse=True)
        selected = candidates[: max(1, int(settings.scanner.max_candidates_per_run))]
        processed: list[dict[str, Any]] = []
        for candidate, bundle in selected:
            if self._shutdown_event.is_set():
                break
            result = await self._dispatch_candidate(run_id, candidate, bundle)
            processed.append(result)

        if data_failures and data_failures >= max(2, scanned // 2):
            degraded_mode = "observe_only"
            degraded_reason = f"{data_failures}/{scanned} symbols failed data quality"
        else:
            degraded_mode = "" if data_failures == 0 else None
            degraded_reason = "" if data_failures == 0 else None

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
        )
        await self._audit(run_id, "run_summary", reason="scanner run summary", payload=funnel)
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
        }
        self._last_status["last_summary"] = summary
        return summary

    async def _scan_watchlist_concurrently(self, run_id: str) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(max(1, int(settings.scanner.max_concurrent_fetches)))

        async def worker(watch_symbol: str) -> dict[str, Any]:
            async with semaphore:
                if self._shutdown_event.is_set():
                    return {"scanned": 0, "data_failures": 0, "filtered": 0, "candidates": []}
                return await self._scan_watch_symbol(run_id, watch_symbol)

        tasks = [asyncio.create_task(worker(symbol)) for symbol in settings.scanner.watchlist]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        normalized: list[dict[str, Any]] = []
        for symbol, result in zip(settings.scanner.watchlist, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[Scanner] Symbol scan failed for {symbol}: {result}")
                await self._audit(run_id, "data_error", watch_symbol=symbol, reason=str(result))
                normalized.append({"scanned": 1, "data_failures": 1, "filtered": 0, "candidates": []})
            else:
                normalized.append(result)
        return normalized

    async def _scan_watch_symbol(self, run_id: str, watch_symbol: str) -> dict[str, Any]:
        try:
            bundle = await self._fetch_bundle_with_retry(watch_symbol)
        except Exception as exc:
            await self._audit(run_id, "data_error", watch_symbol=watch_symbol, reason=str(exc))
            logger.warning(f"[Scanner] Data fetch failed for {watch_symbol}: {exc}")
            return {"scanned": 1, "data_failures": 1, "filtered": 0, "candidates": []}

        mapping = bundle.mapping
        await self._audit(
            run_id,
            "scanned",
            watch_symbol=mapping.watch_symbol,
            exchange_symbol=mapping.exchange_symbol,
            reason="quality_ok" if bundle.quality_passed else ";".join(bundle.quality_reasons),
            payload={"quality": bundle.data_quality, "data_source": mapping.data_source},
        )

        if not bundle.quality_passed:
            return {"scanned": 1, "data_failures": 1, "filtered": 1, "candidates": []}

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
            return {"scanned": 1, "data_failures": 0, "filtered": 1, "candidates": []}

        symbol_candidates = self._build_candidates(bundle)
        if not symbol_candidates:
            await self._audit(
                run_id,
                "filtered",
                watch_symbol=mapping.watch_symbol,
                exchange_symbol=mapping.exchange_symbol,
                reason="no candidate reached pre-scan score",
            )
            return {"scanned": 1, "data_failures": 0, "filtered": 1, "candidates": []}

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
            accepted.append((candidate, bundle))
        return {"scanned": 1, "data_failures": 0, "filtered": 0, "candidates": accepted}

    def _build_candidates(self, bundle: OHLCVBundle) -> list[ScannerCandidate]:
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
            base.fused_timeframes = timeframes
            base.fusion_summary = {
                "enabled": True,
                "timeframes": timeframes,
                "confirmations": confirmations,
                "avg_score": round(avg_score, 2),
                "bonus": round(bonus, 2),
                "conflict_penalty": round(conflict_penalty, 2),
                "opposite_best_score": round(conflict_score, 2),
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
            if base.score >= float(settings.scanner.min_score):
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
        }

    def _weighted(self, name: str, value: float) -> float:
        try:
            factor = float((settings.scanner.score_weights or {}).get(name, 1.0))
        except (TypeError, ValueError):
            factor = 1.0
        return value * factor

    async def _fetch_bundle_with_retry(self, watch_symbol: str) -> OHLCVBundle:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await self.provider.get_bundle(watch_symbol, settings.scanner.timeframes)
            except Exception as exc:
                last_error = exc
                text = str(exc).lower()
                rate_limited = any(key in text for key in ("rate limit", "429", "too many requests"))
                delay = min(8.0, 2.0 ** attempt) if rate_limited else min(3.0, 0.5 * (attempt + 1))
                if attempt >= 2:
                    break
                logger.warning(
                    f"[Scanner] Data fetch retry {attempt + 1}/2 for {watch_symbol} "
                    f"in {delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)
        raise last_error or RuntimeError(f"Failed to fetch scanner bundle for {watch_symbol}")

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
        atr_pct = _safe_float(primary_indicators.get("atr_pct"))
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
        atr_price = max(current * max(atr_pct, 0.05) / 100.0, current * 0.001)

        support = self._best_support_zone(ctx, direction, current, atr_price)
        structure = getattr(ctx, "structure", None)
        trend = str(getattr(structure, "trend", "ranging") or "ranging").lower()
        premium = _safe_float(getattr(ctx, "premium_zone", 0.0))
        discount = _safe_float(getattr(ctx, "discount_zone", 0.0))
        equilibrium = _safe_float(getattr(ctx, "equilibrium", 0.0))

        score = 0.0
        reasons: list[str] = []

        if direction == "long":
            if ema_fast > ema_slow > 0:
                score += self._weighted("ema_alignment", 16)
                reasons.append("EMA bullish alignment")
            if rsi <= float(settings.scanner.rsi_lower):
                score += self._weighted("rsi_extreme", 12)
                reasons.append("RSI oversold")
            if trend == "bullish":
                score += self._weighted("smc_trend", 18)
                reasons.append("SMC trend bullish")
            elif trend == "ranging":
                score += self._weighted("smc_ranging", 8)
            if discount and current <= discount:
                score += self._weighted("price_zone", 22)
                price_zone = "discount"
                reasons.append("price in discount zone")
            elif equilibrium and current <= equilibrium:
                score += self._weighted("price_zone", 12)
                price_zone = "below_equilibrium"
            else:
                price_zone = "premium_or_neutral"
        else:
            if ema_fast < ema_slow and ema_fast > 0 and ema_slow > 0:
                score += self._weighted("ema_alignment", 16)
                reasons.append("EMA bearish alignment")
            if rsi >= float(settings.scanner.rsi_upper):
                score += self._weighted("rsi_extreme", 12)
                reasons.append("RSI overbought")
            if trend == "bearish":
                score += self._weighted("smc_trend", 18)
                reasons.append("SMC trend bearish")
            elif trend == "ranging":
                score += self._weighted("smc_ranging", 8)
            if premium and current >= premium:
                score += self._weighted("price_zone", 22)
                price_zone = "premium"
                reasons.append("price in premium zone")
            elif equilibrium and current >= equilibrium:
                score += self._weighted("price_zone", 12)
                price_zone = "above_equilibrium"
            else:
                price_zone = "discount_or_neutral"

        if atr_pct >= float(settings.scanner.min_atr_pct):
            score += self._weighted("atr", 12)
            reasons.append("ATR volatility acceptable")
        if bundle.bid_ask_spread_pct <= float(settings.scanner.max_spread_pct):
            score += self._weighted("spread", 6)
        if (direction == "long" and macd_hist > 0) or (direction == "short" and macd_hist < 0):
            score += self._weighted("macd_confirmation", 6)
            reasons.append("MACD confirms direction")
        elif macd_hist:
            score -= abs(self._weighted("macd_confirmation", 4))
        if adx >= 18:
            score += self._weighted("adx_confirmation", 4)
            reasons.append("ADX trend strength acceptable")
        if volume_ratio_raw is not None and volume_ratio >= 1.0:
            score += self._weighted("volume_confirmation", 4)
            reasons.append("volume confirms participation")
        elif volume_ratio_raw is not None and volume_ratio < float(settings.scanner.min_volume_ratio):
            score -= abs(self._weighted("volume_confirmation", 6))
        if support:
            score += self._weighted("support_zone", 24)
            reasons.append(f"near {support['type']} support zone")
        else:
            score -= 10

        risk_score = _safe_float(getattr(ctx, "risk_score", 0.5), 0.5)
        timing_score = _safe_float(getattr(ctx, "entry_timing_score", 0.5), 0.5)
        score += self._weighted("risk", max(0.0, (1.0 - risk_score) * 8.0))
        score += self._weighted("timing", max(0.0, timing_score * 8.0))

        if settings.scanner.ema200_enabled and ema200 > 0:
            if direction == "long" and current > ema200:
                score += self._weighted("ema200_alignment", 10)
                reasons.append("EMA200 bullish alignment")
            elif direction == "short" and current < ema200:
                score += self._weighted("ema200_alignment", 10)
                reasons.append("EMA200 bearish alignment")
            else:
                penalty = self._weighted("ema200_conflict", 15)
                score -= penalty
                reasons.append(f"EMA200 conflict penalized ({-penalty:.0f})")

        if htf_trend and settings.scanner.htf_conflict_enabled:
            htf_bullish = htf_trend == "bullish"
            htf_bearish = htf_trend == "bearish"
            conflicting = (direction == "long" and htf_bearish) or (direction == "short" and htf_bullish)
            if conflicting:
                penalty = self._weighted("htf_conflict", 20)
                score -= penalty
                reasons.append(f"HTF structure conflict ({htf_trend}) penalized ({-penalty:.0f})")

        if vwap > 0:
            if direction == "long" and current > vwap:
                score += self._weighted("vwap", 6)
                reasons.append("price above VWAP")
            elif direction == "short" and current < vwap:
                score += self._weighted("vwap", 6)
                reasons.append("price below VWAP")
            elif vwap_dist is not None:
                score -= abs(self._weighted("vwap", 4))

        if poc > 0:
            if (direction == "long" and current > poc) or (direction == "short" and current < poc):
                score += self._weighted("poc", 5)
                reasons.append("price favorable to POC")

        if oi_change is not None:
            oi_bullish = oi_change > 3.0
            oi_bearish = oi_change < -3.0
            if direction == "long" and oi_bullish:
                score += self._weighted("oi_confirmation", 6)
                reasons.append(f"OI rising {oi_change:+.1f}% confirms long")
            elif direction == "short" and oi_bearish:
                score += self._weighted("oi_confirmation", 6)
                reasons.append(f"OI falling {oi_change:+.1f}% confirms short")
            elif direction == "long" and oi_bearish:
                score -= self._weighted("oi_divergence", 5)
                reasons.append(f"OI divergence: OI {oi_change:+.1f}% vs long signal")

        if settings.scanner.regime_filter_enabled:
            if regime == "trending":
                score += self._weighted("regime_trending", 4)
                reasons.append("trending market regime")
            elif regime == "ranging":
                score -= self._weighted("regime_ranging", 6)
                reasons.append("ranging market regime penalized")

        score = max(0.0, min(100.0, score))

        fusion_floor = max(0.0, float(settings.scanner.min_score) - max(12.0, float(settings.scanner.mtf_confirmation_bonus) * 2.0))
        if score < fusion_floor:
            return None

        support_mid = _safe_float((support or {}).get("midpoint"), current)
        setup_type = str((support or {}).get("type") or "indicator_smc")
        price_zone_key = f"{price_zone}:{round(support_mid, 2)}"
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
            "trend": trend,
            "risk_score": round(risk_score, 4),
            "entry_timing_score": round(timing_score, 4),
            "timing_recommendation": getattr(ctx, "timing_recommendation", ""),
            "premium_zone": premium,
            "discount_zone": discount,
            "equilibrium": equilibrium,
            "zone": price_zone,
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
            score=round(score, 2),
            setup_type=setup_type,
            price_zone=price_zone_key,
            setup_hash=setup_hash,
            reasons=reasons,
            indicator_summary={
                "rsi": rsi,
                "atr_pct": atr_pct,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "ema200": ema200,
                "macd_hist": macd_hist,
                "adx": adx,
                "volume_ratio": volume_ratio,
                "spread_pct": bundle.bid_ask_spread_pct,
                "vwap": vwap,
                "vwap_distance_pct": vwap_dist,
                "poc": poc,
                "regime": regime,
                "oi_change_pct": oi_change,
                "htf_trend": htf_trend or "",
            },
            smc_summary=smc_summary,
            quality={"reasons": bundle.quality_reasons, "passed": bundle.quality_passed},
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
                    PositionModel.status.in_(["open", "pending"])
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

            cooldown_ttl = self._symbol_cooldown_ttl_for_result(result)
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
                payload={"result": result, "ai_used": ai_used, "cooldown_ttl_secs": cooldown_ttl},
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
            }

    def _symbol_cooldown_ttl_for_result(self, result: dict[str, Any]) -> int:
        status = str(result.get("status") or "").lower().strip()
        reason = str(result.get("reason") or "").lower()
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        recommendation = str(analysis.get("recommendation") or "").lower().strip()
        if status in {"duplicate", "blocked", "error"}:
            return max(0, int(settings.scanner.blocked_symbol_cooldown_secs))
        if status == "rejected" or recommendation in {"reject", "hold"} or "rejected" in reason:
            return max(0, int(settings.scanner.rejected_symbol_cooldown_secs))
        if status == "observed" and not bool(result.get("would_execute")):
            return max(0, int(settings.scanner.rejected_symbol_cooldown_secs))
        return max(0, int(settings.scanner.symbol_cooldown_secs))

    async def _validate_live_market(self, candidate: ScannerCandidate) -> tuple[bool, str, dict[str, Any]]:
        """Fail closed before AI calls if a live scanner symbol is not tradable."""
        if str(settings.scanner.mode).lower().strip() != "live":
            return True, "", {}
        if not settings.exchange.live_trading:
            return False, "Scanner live mode requires global LIVE_TRADING=true", {}
        whitelist = {str(item).upper().strip() for item in settings.scanner.live_symbol_whitelist}
        if candidate.exchange_symbol.upper().strip() not in whitelist:
            return False, "Scanner live mode blocked: symbol is not in SCANNER_LIVE_SYMBOL_WHITELIST", {}
        exchange_name = str(candidate.exchange_name or settings.exchange.name).lower().strip()
        market_type = str(candidate.market_type or settings.exchange.market_type).lower().strip()
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
        async with db_manager.async_session_factory() as session:
            await record_scanner_audit(
                session,
                scope=self.scope,
                run_id=run_id,
                event_type=event_type,
                watch_symbol=watch_symbol,
                exchange_symbol=exchange_symbol,
                direction=direction,
                score=score,
                setup_hash=setup_hash,
                reason=reason,
                payload=payload,
            )
            await session.commit()


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

"""
Signal Server - Webhook Router
Handles TradingView webhook signals.

Security:
- Payload secret (required, TradingView compatible)
- Primary security relies on the 'secret' field in JSON payload
- Timestamp-based replay protection (±5 minute window)
- Nonce-based deduplication for additional replay prevention

Processing:
- Returns 202 Accepted immediately to prevent TradingView timeout
- Actual processing runs in background task
- Fingerprint deduplication prevents duplicate execution
"""
import asyncio
import hashlib
import hmac
import json
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_admin_setting, get_db
from core.request_utils import client_ip as get_client_ip
from core.security import is_placeholder_webhook_secret
from models import TradingViewSignal
from services.signal_processor import SignalProcessor, compute_webhook_fingerprint

router = APIRouter(prefix="", tags=["webhook"])

# Match TradingView's practical delivery/clock-skew tolerance while nonce
# deduplication prevents a valid payload from being executed twice.
_WEBHOOK_REPLAY_WINDOW_SECS = 300


async def _verify_hmac_signature(request: Request, raw_body: bytes) -> None:
    """Verify HMAC signature from request header if enabled.

    This provides an additional layer of security beyond the payload secret.
    TradingView does NOT support HMAC headers, so this is optional.
    For custom integrations, enable WEBHOOK_HMAC_HEADER_ENABLED=true.
    """
    if not settings.server.webhook_hmac_header_enabled:
        return

    hmac_secret = settings.server.webhook_hmac_secret
    if not hmac_secret:
        logger.warning("[Webhook] HMAC header enabled but WEBHOOK_HMAC_SECRET not set")
        raise HTTPException(500, "HMAC verification misconfigured")

    header_name = settings.server.webhook_hmac_header_name
    signature = request.headers.get(header_name, "")

    if not signature:
        raise HTTPException(401, f"Missing HMAC signature header: {header_name}")

    try:
        expected = hmac.new(
            hmac_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning(f"[Webhook] Invalid HMAC signature from {get_client_ip(request)}")
            raise HTTPException(401, "Invalid HMAC signature")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Webhook] HMAC verification error: {e}")
        raise HTTPException(500, "HMAC verification failed") from e

_NONCE_CACHE: "OrderedDict[str, float]" = OrderedDict()
_NONCE_CACHE_MAX_SIZE = 10000
_NONCE_CACHE_CLEANUP_INTERVAL = 3600
_last_nonce_cleanup: float = 0.0
_nonce_lock = asyncio.Lock()

# P2-9: IP-based rate limiting for webhook endpoint
_WEBHOOK_IP_RATE_LIMITS: dict[str, list[float]] = {}
_WEBHOOK_IP_RATE_MAX = 60  # Maximum requests per window
_WEBHOOK_IP_RATE_WINDOW = 60.0  # Window in seconds
_WEBHOOK_IP_RATE_MAX_ENTRIES = 5000  # Cap to prevent memory leak
_WEBHOOK_IP_RATE_CLEANUP_THRESHOLD = 6000  # Trigger cleanup when exceeding this
_webhook_ip_rate_lock = asyncio.Lock()  # P1-FIX: Protect concurrent access

# Redis nonce cache for multi-process deployments
_redis_nonce_available: bool | None = None
_redis_nonce_client: "Any | None" = None


async def _get_redis_nonce_client():
    """Lazily get Redis client for nonce deduplication.

    Uses the same Redis connection as the cache layer. Falls back to in-memory
    when Redis is disabled or unavailable, which ensures single-process
    deployments still have replay protection.
    """
    global _redis_nonce_available, _redis_nonce_client
    if _redis_nonce_available is not None:
        return _redis_nonce_client if _redis_nonce_available else None

    from core.config import settings
    if not settings.redis.enabled:
        _redis_nonce_available = False
        return None

    try:
        from core.cache import cache as cache_manager
        redis_obj = getattr(cache_manager, "_redis", None)
        if redis_obj and getattr(redis_obj, "is_connected", lambda: False)():
            _redis_nonce_client = redis_obj
            _redis_nonce_available = True
            logger.debug("[Webhook] Redis nonce backend connected")
            return _redis_nonce_client
        if redis_obj:
            await redis_obj._get_client()
            if redis_obj.is_connected():
                _redis_nonce_client = redis_obj
                _redis_nonce_available = True
                logger.debug("[Webhook] Redis nonce backend connected (lazy)")
                return _redis_nonce_client
    except Exception:
        pass

    _redis_nonce_available = False
    logger.warning(
        "[Webhook] Redis nonce backend unavailable — using in-memory fallback. "
        "For multi-process deployments, enable REDIS_ENABLED=true."
    )
    return None


def _parse_webhook_timestamp(value: Any) -> float | None:
    """Parse an optional webhook timestamp into epoch seconds."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
        else:
            text = str(value).strip()
            if not text:
                raise ValueError("empty timestamp")
            try:
                timestamp = float(text)
            except ValueError:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                timestamp = dt.timestamp()
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000.0
        if timestamp <= 0:
            raise ValueError("timestamp must be positive")
        return timestamp
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(400, "Webhook timestamp must be epoch seconds, epoch milliseconds, or ISO-8601") from exc


def _durable_queue_payload(body: dict[str, Any], signal: TradingViewSignal) -> dict[str, Any]:
    """Freeze validation-time defaults before a signal enters the durable queue."""
    payload = dict(body)
    payload["timestamp"] = signal.timestamp.isoformat()
    return payload


def _parse_webhook_nonce(value: Any) -> str | None:
    """Parse and validate an optional webhook nonce."""
    if value is None:
        return None
    nonce = str(value or "").strip()
    if not nonce:
        return None
    if len(nonce) > 128 or any(ord(ch) < 32 or ord(ch) > 126 for ch in nonce):
        raise HTTPException(400, "Webhook nonce must be 1-128 printable ASCII characters")
    return nonce


async def _check_replay_protection(nonce: str, timestamp: float, scope: str) -> bool:
    """Validate replay fields and report whether this nonce was seen.

    Uses a five-minute delivery window so TradingView retries and ordinary
    network/clock skew do not invalidate an otherwise authenticated nonce.

    The cache is advisory. Durable fingerprint reservation in the database is
    authoritative, so a transient database failure cannot poison the nonce
    cache and prevent TradingView from retrying a signal.
    """
    now = time.time()
    time_diff = abs(now - timestamp)

    if time_diff > _WEBHOOK_REPLAY_WINDOW_SECS:
        logger.warning(
            f"[Webhook] Timestamp expired: {time_diff:.1f}s old (max {_WEBHOOK_REPLAY_WINDOW_SECS}s). "
            f"Possible replay attack from scope '{scope}'."
        )
        raise HTTPException(401, f"Webhook timestamp expired ({time_diff:.1f}s) — possible replay attack")

    # P0-FIX: Validate nonce format to prevent injection
    if not nonce or len(nonce) > 128:
        raise HTTPException(400, "Invalid nonce length")
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in nonce):
        raise HTTPException(400, "Nonce contains invalid characters")

    nonce_key = hashlib.sha256(f"{scope}:{nonce}".encode()).hexdigest()

    # Try Redis first for multi-process safety
    redis_client = await _get_redis_nonce_client()
    if redis_client:
        try:
            client = await redis_client._get_client()
            if client:
                redis_key = f"webhook:nonce:{nonce_key}"
                existing = await client.get(redis_key)
                if existing:
                    logger.warning(f"[Webhook] Duplicate nonce detected in Redis: {nonce[:16]}...")
                    return True
                # Keep Redis and the in-memory fallback on the same contract.
                await client.setex(redis_key, _WEBHOOK_REPLAY_WINDOW_SECS + 10, str(now))
                return False
        except Exception:
            logger.warning("[Webhook] Redis nonce check failed, falling back to in-memory")
        # Fall through to in-memory

    # In-memory fallback
    async with _nonce_lock:
        global _last_nonce_cleanup
        if now - _last_nonce_cleanup > _NONCE_CACHE_CLEANUP_INTERVAL:
            cutoff = now - _WEBHOOK_REPLAY_WINDOW_SECS
            expired = [k for k, v in _NONCE_CACHE.items() if v < cutoff]
            for k in expired:
                _NONCE_CACHE.pop(k, None)
            _last_nonce_cleanup = now

        if nonce_key in _NONCE_CACHE:
            logger.warning(f"[Webhook] Duplicate nonce detected in memory: {nonce[:16]}...")
            return True

        # P4-FIX: Use OrderedDict with LRU eviction on every insert to
        # guarantee cache stays bounded even under sustained high traffic.
        _NONCE_CACHE[nonce_key] = now
        _NONCE_CACHE.move_to_end(nonce_key)
        if len(_NONCE_CACHE) > _NONCE_CACHE_MAX_SIZE:
            # First clean expired entries
            cutoff = now - _WEBHOOK_REPLAY_WINDOW_SECS
            expired = [k for k, v in _NONCE_CACHE.items() if v < cutoff]
            for k in expired:
                _NONCE_CACHE.pop(k, None)
            # Then evict oldest entries to enforce hard cap (LRU)
            while len(_NONCE_CACHE) > _NONCE_CACHE_MAX_SIZE:
                _NONCE_CACHE.popitem(last=False)
    return False


async def _check_ip_rate_limit(client_ip: str) -> None:
    """P2-9: IP-based rate limiting for webhook endpoint.

    Prevents abuse by limiting requests per IP address.
    Uses a sliding window approach with in-memory tracking.
    Includes bounded cache size and periodic cleanup to prevent memory leak.
    P1-FIX: Protected by asyncio.Lock for concurrent access safety.
    """
    global _WEBHOOK_IP_RATE_LIMITS
    now = time.time()
    cutoff = now - _WEBHOOK_IP_RATE_WINDOW

    async with _webhook_ip_rate_lock:
        # Periodic global cleanup to prevent memory leak
        if len(_WEBHOOK_IP_RATE_LIMITS) > _WEBHOOK_IP_RATE_CLEANUP_THRESHOLD:
            expired_ips = [
                ip for ip, ts_list in _WEBHOOK_IP_RATE_LIMITS.items()
                if not ts_list or ts_list[-1] < cutoff
            ]
            for ip in expired_ips:
                _WEBHOOK_IP_RATE_LIMITS.pop(ip, None)

            # If still over limit, evict oldest entries
            if len(_WEBHOOK_IP_RATE_LIMITS) > _WEBHOOK_IP_RATE_MAX_ENTRIES:
                sorted_ips = sorted(
                    _WEBHOOK_IP_RATE_LIMITS.items(),
                    key=lambda item: item[1][-1] if item[1] else 0,
                )
                to_remove = len(_WEBHOOK_IP_RATE_LIMITS) - _WEBHOOK_IP_RATE_MAX_ENTRIES
                for ip, _ in sorted_ips[:to_remove]:
                    _WEBHOOK_IP_RATE_LIMITS.pop(ip, None)

        # Clean up old entries for this IP
        timestamps = _WEBHOOK_IP_RATE_LIMITS.get(client_ip, [])
        timestamps = [ts for ts in timestamps if ts > cutoff]
        _WEBHOOK_IP_RATE_LIMITS[client_ip] = timestamps

        # Check rate limit
        if len(timestamps) >= _WEBHOOK_IP_RATE_MAX:
            raise HTTPException(429, "Rate limit exceeded - too many requests")

        # Record this request
        timestamps.append(now)
        _WEBHOOK_IP_RATE_LIMITS[client_ip] = timestamps


@router.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive and process TradingView webhook signals.

    Supports both admin webhook secret and per-user secrets.

    TradingView Compatibility:
    TradingView only sends the 'secret' field in JSON payload.
    It does NOT support HMAC signature headers.

    Security:
    - Payload secret is REQUIRED (primary security for TradingView)
    - In live_trading mode, payload secret must be strong (not placeholder)

    Processing:
    - Returns 202 Accepted immediately (within ~100ms)
    - Actual processing runs in background to avoid TradingView timeout
    - Fingerprint deduplication prevents duplicate signals
    """
    try:
        raw_body = await request.body()
        body = json.loads(raw_body)
    except json.JSONDecodeError as err:
        logger.error(f"[Webhook] Invalid JSON: {err}")
        raise HTTPException(400, "Invalid JSON payload") from err

    # Verify HMAC signature if enabled (optional security layer)
    await _verify_hmac_signature(request, raw_body)

    secret = body.get("secret", "").strip()
    if not secret:
        logger.warning("[Webhook] Missing webhook secret in payload")
        raise HTTPException(401, "Missing webhook secret in payload")

    client_ip = get_client_ip(request)

    # P2-9: IP-based rate limiting
    await _check_ip_rate_limit(client_ip)

    try:
        signal = TradingViewSignal(**body)
    except (ValueError, TypeError, KeyError) as err:
        logger.error(f"[Webhook] Invalid signal: {err}")
        raise HTTPException(400, f"Invalid signal: {err}") from err
    except Exception as err:
        logger.error(f"[Webhook] Unexpected error validating signal: {err}")
        raise HTTPException(400, f"Invalid signal: {err}") from err

    user = await _find_user_by_secret(db, secret)
    user_id = user.id if user else None

    if not user_id:
        admin_secret = await get_admin_setting(db, "webhook_secret", settings.server.webhook_secret)

        if settings.exchange.live_trading:
            if is_placeholder_webhook_secret(admin_secret):
                logger.error(
                    "[Security] LIVE_TRADING enabled but webhook secret is placeholder/weak. "
                    "Please set a strong WEBHOOK_SECRET in environment."
                )
                raise HTTPException(401, "Webhook secret not configured for live trading")

        admin_secret_hash = hashlib.sha256(admin_secret.encode()).hexdigest()
        provided_hash = hashlib.sha256(secret.encode()).hexdigest()
        if not hmac.compare_digest(provided_hash, admin_secret_hash):
            logger.warning(f"[Webhook] Invalid secret from {client_ip}")
            raise HTTPException(401, "Invalid webhook secret")

    fingerprint = compute_webhook_fingerprint(body, user_id)
    timestamp = _parse_webhook_timestamp(body.get("timestamp"))
    nonce = _parse_webhook_nonce(body.get("nonce"))
    replay_scope = user_id or "admin"
    if (timestamp is None) != (nonce is None):
        raise HTTPException(
            400,
            "Webhook timestamp and nonce must either both be provided or both be omitted",
        )
    if timestamp is not None and nonce is not None:
        nonce_seen = await _check_replay_protection(nonce, timestamp, replay_scope)
    else:
        short_fingerprint = hashlib.sha256(f"{replay_scope}:{fingerprint}".encode()).hexdigest()[:16]
        nonce_seen = await _check_replay_protection(
            short_fingerprint,
            time.time(),
            replay_scope,
        )

    # Persist the validation-time timestamp even when the sender omitted one.
    # Reconstructing the model later in a durable worker would otherwise assign
    # a fresh timestamp and allow a delayed entry signal to bypass the age cap.
    persisted_body = _durable_queue_payload(body, signal)

    processor = SignalProcessor(db)
    reservation = await processor._reserve_webhook_event(
        fingerprint=fingerprint,
        signal=signal,
        user_id=user_id,
        client_ip=client_ip,
        payload=persisted_body,
    )
    if reservation is None:
        await db.commit()
        return JSONResponse(
            status_code=202,
            content={"status": "duplicate", "message": "Signal already received"},
        )
    await db.commit()
    if nonce_seen:
        logger.info(
            f"[Webhook] Previously seen nonce accepted for durable retry: "
            f"event={reservation.id}"
        )

    background_tasks.add_task(
        _process_webhook_background,
        signal=signal,
        user_id=user_id,
        client_ip=client_ip,
        raw_body=persisted_body,
        reserved_event_id=reservation.id,
    )

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "event_id": reservation.id,
            "message": "Signal durably queued for processing",
        },
    )


async def _process_webhook_background(
    signal: TradingViewSignal,
    user_id: str | None,
    client_ip: str,
    raw_body: dict,
    reserved_event_id: str | None = None,
):
    """Kick the durable worker for low latency.

    The event is already committed. If this in-process kick is lost, the
    scheduler reclaims the same database row after restart.
    """
    if not reserved_event_id:
        logger.error(f"[Webhook] Durable event id missing for {signal.ticker}")
        return
    from services.webhook_worker import process_webhook_event

    result = await process_webhook_event(reserved_event_id)
    logger.info(
        f"[Webhook] Durable processing update: event={reserved_event_id}, "
        f"status={result.get('status')}"
    )


async def _find_user_by_secret(db: AsyncSession, secret: str):
    """Find user by webhook secret.

    Uses constant-time comparison to prevent timing side-channel attacks
    that could enumerate valid user webhook secrets.
    """
    from sqlalchemy import select

    from core.database import UserModel
    from core.security import webhook_secret_hash

    secret_hash = webhook_secret_hash(secret)
    result = await db.execute(
        select(UserModel).where(
            UserModel.webhook_secret_hash == secret_hash,
            UserModel.is_active,
            UserModel.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        _dummy_hash = hashlib.sha256(b"timing-attack-mitigation-dummy").hexdigest()
        hmac.compare_digest(secret_hash, _dummy_hash)

    return user

"""
Login brute-force protection.
Tracks failed login attempts per IP and locks out after threshold.
Supports Redis persistence when available to survive service restarts.
"""
import json
import threading
import time

from loguru import logger

from core.config import settings

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900  # 15 minutes
_WINDOW_SECONDS = 600   # 10-minute sliding window

_lock = threading.Lock()
_attempts: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}

_redis_client = None


def _init_redis():
    """Initialize Redis client if available."""
    global _redis_client
    if _redis_client is not None:
        return
    if not settings.redis.enabled:
        return
    try:
        import redis
        _redis_client = redis.from_url(settings.redis.url, decode_responses=True)
        logger.info("[LoginGuard] Redis persistence enabled")
    except Exception as exc:
        logger.warning(f"[LoginGuard] Redis connection failed, using in-memory fallback: {exc}")
        _redis_client = None


def _redis_key_attempts(ip: str) -> str:
    return f"login_guard:attempts:{ip}"


def _redis_key_lockout(ip: str) -> str:
    return f"login_guard:lockout:{ip}"


def _cleanup_old_entries() -> None:
    """Remove expired entries to prevent memory growth."""
    now = time.monotonic()
    expired_lockouts = [k for k, v in _lockouts.items() if now - v > _LOCKOUT_SECONDS]
    for k in expired_lockouts:
        _lockouts.pop(k, None)
        _attempts.pop(k, None)
    expired_attempts = [
        k for k, v in _attempts.items()
        if k not in _lockouts and (not v or now - v[-1] > _WINDOW_SECONDS)
    ]
    for k in expired_attempts:
        _attempts.pop(k, None)


def is_locked_out(ip: str) -> bool:
    """Check if an IP is currently locked out."""
    _init_redis()
    
    if _redis_client:
        try:
            lockout_data = _redis_client.get(_redis_key_lockout(ip))
            if lockout_data:
                lockout_time = float(lockout_data)
                remaining = _LOCKOUT_SECONDS - (time.time() - lockout_time)
                if remaining > 0:
                    return True
                _redis_client.delete(_redis_key_lockout(ip))
                _redis_client.delete(_redis_key_attempts(ip))
            return False
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis read failed, falling back to memory: {exc}")

    with _lock:
        lockout_time = _lockouts.get(ip)
        if lockout_time is None:
            return False
        if time.monotonic() - lockout_time > _LOCKOUT_SECONDS:
            _lockouts.pop(ip, None)
            _attempts.pop(ip, None)
            return False
        return True


def remaining_lockout_seconds(ip: str) -> int:
    """Return seconds remaining in lockout, or 0 if not locked."""
    _init_redis()
    
    if _redis_client:
        try:
            lockout_data = _redis_client.get(_redis_key_lockout(ip))
            if lockout_data:
                lockout_time = float(lockout_data)
                remaining = _LOCKOUT_SECONDS - (time.time() - lockout_time)
                return max(0, int(remaining))
            return 0
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis read failed, falling back to memory: {exc}")

    with _lock:
        lockout_time = _lockouts.get(ip)
        if lockout_time is None:
            return 0
        remaining = _LOCKOUT_SECONDS - (time.monotonic() - lockout_time)
        return max(0, int(remaining))


def record_failed_attempt(ip: str) -> int | None:
    """
    Record a failed login attempt.
    Returns the number of remaining attempts, or None if now locked out.
    """
    _init_redis()
    now = time.time()
    
    if _redis_client:
        try:
            attempts_key = _redis_key_attempts(ip)
            lockout_key = _redis_key_lockout(ip)
            
            existing = _redis_client.get(attempts_key)
            attempts = json.loads(existing) if existing else []
            cutoff = now - _WINDOW_SECONDS
            attempts = [t for t in attempts if t > cutoff]
            attempts.append(now)
            
            if len(attempts) >= _MAX_ATTEMPTS:
                _redis_client.setex(lockout_key, _LOCKOUT_SECONDS, str(now))
                _redis_client.delete(attempts_key)
                logger.warning(f"[LoginGuard] IP {ip} locked out after {_MAX_ATTEMPTS} failed attempts (Redis)")
                return None
            
            _redis_client.setex(attempts_key, _WINDOW_SECONDS, json.dumps(attempts))
            remaining = _MAX_ATTEMPTS - len(attempts)
            return remaining
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis write failed, falling back to memory: {exc}")

    with _lock:
        _cleanup_old_entries()
        attempts = _attempts.setdefault(ip, [])
        cutoff = time.monotonic() - _WINDOW_SECONDS
        attempts[:] = [t for t in attempts if t > cutoff]
        attempts.append(time.monotonic())

        if len(attempts) >= _MAX_ATTEMPTS:
            _lockouts[ip] = time.monotonic()
            logger.warning(f"[LoginGuard] IP {ip} locked out after {_MAX_ATTEMPTS} failed attempts")
            return None

        remaining = _MAX_ATTEMPTS - len(attempts)
        return remaining


def record_successful_login(ip: str) -> None:
    """Clear failed attempts on successful login."""
    _init_redis()
    
    if _redis_client:
        try:
            _redis_client.delete(_redis_key_attempts(ip))
            _redis_client.delete(_redis_key_lockout(ip))
            return
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis delete failed, falling back to memory: {exc}")

    with _lock:
        _attempts.pop(ip, None)
        _lockouts.pop(ip, None)


def get_stats() -> dict:
    """Return current lockout statistics."""
    _init_redis()
    
    if _redis_client:
        try:
            lockout_keys = _redis_client.keys("login_guard:lockout:*")
            attempt_keys = _redis_client.keys("login_guard:attempts:*")
            return {
                "tracked_ips": len(attempt_keys),
                "locked_out_ips": len(lockout_keys),
                "backend": "redis",
            }
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis stats failed, falling back to memory: {exc}")

    with _lock:
        return {
            "tracked_ips": len(_attempts),
            "locked_out_ips": len(_lockouts),
            "backend": "memory",
        }
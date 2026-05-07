"""
Login brute-force protection.
Tracks failed login attempts per IP and locks out after threshold.
Supports Redis persistence when available to survive service restarts.

H2-FIX: Supports separate counters for password-phase and 2FA-phase failures
to prevent attackers from exhausting the counter across phases.
"""
import json
import threading
import time

from loguru import logger

from core.config import settings

_MAX_ATTEMPTS = settings.rate_limit.login_max_attempts
_LOCKOUT_SECONDS = 900
_WINDOW_SECONDS = settings.rate_limit.login_window_secs

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


def _redis_key_attempts(ip: str, phase: str = "password") -> str:
    return f"login_guard:{phase}:attempts:{ip}"


def _redis_key_lockout(ip: str, phase: str = "password") -> str:
    return f"login_guard:{phase}:lockout:{ip}"


def _cleanup_old_entries() -> None:
    """Remove expired entries to prevent memory growth."""
    now = time.time()
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


def is_locked_out(ip: str, phase: str = "password") -> bool:
    """Check if an IP is currently locked out for a specific phase."""
    _init_redis()
    lockout_key = _redis_key_lockout(ip, phase)
    attempts_key = _redis_key_attempts(ip, phase)

    if _redis_client:
        try:
            lockout_data = _redis_client.get(lockout_key)
            if lockout_data:
                lockout_time = float(lockout_data)
                remaining = _LOCKOUT_SECONDS - (time.time() - lockout_time)
                if remaining > 0:
                    return True
                _redis_client.delete(lockout_key)
                _redis_client.delete(attempts_key)
            return False
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis read failed, falling back to memory: {exc}")

    with _lock:
        composite_key = f"{phase}:{ip}"
        lockout_time = _lockouts.get(composite_key)
        if lockout_time is None:
            return False
        if time.time() - lockout_time > _LOCKOUT_SECONDS:
            _lockouts.pop(composite_key, None)
            _attempts.pop(composite_key, None)
            return False
        return True


def remaining_lockout_seconds(ip: str, phase: str = "password") -> int:
    """Return seconds remaining in lockout, or 0 if not locked."""
    _init_redis()
    lockout_key = _redis_key_lockout(ip, phase)

    if _redis_client:
        try:
            lockout_data = _redis_client.get(lockout_key)
            if lockout_data:
                lockout_time = float(lockout_data)
                remaining = _LOCKOUT_SECONDS - (time.time() - lockout_time)
                return max(0, int(remaining))
            return 0
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis read failed, falling back to memory: {exc}")

    with _lock:
        composite_key = f"{phase}:{ip}"
        lockout_time = _lockouts.get(composite_key)
        if lockout_time is None:
            return 0
        remaining = _LOCKOUT_SECONDS - (time.time() - lockout_time)
        return max(0, int(remaining))


def record_failed_attempt(ip: str, phase: str = "password") -> int | None:
    """
    Record a failed login attempt for a specific phase.
    Returns the number of remaining attempts, or None if now locked out.
    """
    _init_redis()
    now = time.time()
    attempts_key = _redis_key_attempts(ip, phase)
    lockout_key = _redis_key_lockout(ip, phase)
    composite_key = f"{phase}:{ip}"

    if _redis_client:
        try:
            existing = _redis_client.get(attempts_key)
            attempts = json.loads(existing) if existing else []
            cutoff = now - _WINDOW_SECONDS
            attempts = [t for t in attempts if t > cutoff]
            attempts.append(now)

            if len(attempts) >= _MAX_ATTEMPTS:
                _redis_client.setex(lockout_key, _LOCKOUT_SECONDS, str(now))
                _redis_client.delete(attempts_key)
                logger.warning(f"[LoginGuard] IP {ip} locked out after {_MAX_ATTEMPTS} failed {phase} attempts (Redis)")
                return None

            _redis_client.setex(attempts_key, _WINDOW_SECONDS, json.dumps(attempts))
            remaining = _MAX_ATTEMPTS - len(attempts)
            return remaining
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis write failed, falling back to memory: {exc}")

    with _lock:
        _cleanup_old_entries()
        attempts = _attempts.setdefault(composite_key, [])
        cutoff = time.time() - _WINDOW_SECONDS
        attempts[:] = [t for t in attempts if t > cutoff]
        attempts.append(time.time())

        if len(attempts) >= _MAX_ATTEMPTS:
            _lockouts[composite_key] = time.time()
            logger.warning(f"[LoginGuard] IP {ip} locked out after {_MAX_ATTEMPTS} failed {phase} attempts")
            return None

        remaining = _MAX_ATTEMPTS - len(attempts)
        return remaining


def record_successful_login(ip: str) -> None:
    """Clear failed attempts for all phases on successful login."""
    _init_redis()

    if _redis_client:
        try:
            for phase in ("password", "2fa"):
                _redis_client.delete(_redis_key_attempts(ip, phase))
                _redis_client.delete(_redis_key_lockout(ip, phase))
            return
        except Exception as exc:
            logger.debug(f"[LoginGuard] Redis delete failed, falling back to memory: {exc}")

    with _lock:
        for phase in ("password", "2fa"):
            composite_key = f"{phase}:{ip}"
            _attempts.pop(composite_key, None)
            _lockouts.pop(composite_key, None)


def get_stats() -> dict:
    """Return current lockout statistics."""
    _init_redis()

    if _redis_client:
        try:
            total_attempts = 0
            total_lockouts = 0
            for phase in ("password", "2fa"):
                lockout_keys = _redis_client.keys(f"login_guard:{phase}:lockout:*")
                attempt_keys = _redis_client.keys(f"login_guard:{phase}:attempts:*")
                total_lockouts += len(lockout_keys)
                total_attempts += len(attempt_keys)
            return {
                "tracked_ips": total_attempts,
                "locked_out_ips": total_lockouts,
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

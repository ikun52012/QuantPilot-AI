"""
Login brute-force protection.
Tracks failed login attempts per IP and locks out after threshold.
"""
import time
import threading
from typing import Optional

from loguru import logger


_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900  # 15 minutes
_WINDOW_SECONDS = 600   # 10-minute sliding window

_lock = threading.Lock()
_attempts: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}


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
    with _lock:
        lockout_time = _lockouts.get(ip)
        if lockout_time is None:
            return 0
        remaining = _LOCKOUT_SECONDS - (time.monotonic() - lockout_time)
        return max(0, int(remaining))


def record_failed_attempt(ip: str) -> Optional[int]:
    """
    Record a failed login attempt.
    Returns the number of remaining attempts, or None if now locked out.
    """
    now = time.monotonic()
    with _lock:
        _cleanup_old_entries()
        attempts = _attempts.setdefault(ip, [])
        # Remove attempts outside the window
        cutoff = now - _WINDOW_SECONDS
        attempts[:] = [t for t in attempts if t > cutoff]
        attempts.append(now)

        if len(attempts) >= _MAX_ATTEMPTS:
            _lockouts[ip] = now
            logger.warning(f"[LoginGuard] IP {ip} locked out after {_MAX_ATTEMPTS} failed attempts")
            return None

        remaining = _MAX_ATTEMPTS - len(attempts)
        return remaining


def record_successful_login(ip: str) -> None:
    """Clear failed attempts on successful login."""
    with _lock:
        _attempts.pop(ip, None)
        _lockouts.pop(ip, None)


def get_stats() -> dict:
    """Return current lockout statistics."""
    with _lock:
        return {
            "tracked_ips": len(_attempts),
            "locked_out_ips": len(_lockouts),
        }

# Core package - provides centralized imports for the new architecture
"""
Core modules for Signal Server v4.1

Usage:
    from core.config import settings
    from core.database import db_manager, get_db
    from core.auth import get_current_user, require_admin
    from core.security import hash_password, verify_password
    from core.cache import cache
    from core.middleware import setup_middleware
    from core.metrics import metrics_endpoint
"""

from core.config import settings
from core.database import db_manager, get_db

__all__ = [
    "settings",
    "db_manager",
    "get_db",
]

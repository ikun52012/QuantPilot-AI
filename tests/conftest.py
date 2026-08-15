"""
P4-FIX: QuantPilot Test Configuration
Pytest configuration with fixtures for unit and integration tests.
"""
import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Configure an isolated runtime data root before application modules are
# imported during test collection. This prevents risk trackers, ghost state,
# caches, encryption material, and backups from touching the real data volume.
_PYTEST_DATA_DIR_OVERRIDE = os.getenv("PYTEST_RUNTIME_DATA_DIR", "").strip()
_PYTEST_DATA_DIR = (
    Path(_PYTEST_DATA_DIR_OVERRIDE).expanduser().resolve(strict=False)
    if _PYTEST_DATA_DIR_OVERRIDE
    else (Path.cwd() / ".test_tmp" / f"pytest-runtime-{os.getpid()}").resolve(strict=False)
)
os.environ["DATA_DIR"] = str(_PYTEST_DATA_DIR)
if os.getenv("TEST_DATABASE_URL", "").strip() and os.getenv(
    "ALLOW_EXTERNAL_TEST_DATABASE", "false"
).lower() == "true":
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{(_PYTEST_DATA_DIR / 'server.db').as_posix()}"
    )


def pytest_sessionfinish(session, exitstatus):
    """Remove only the automatically-created test runtime directory."""
    if _PYTEST_DATA_DIR_OVERRIDE:
        return
    test_root = (Path.cwd() / ".test_tmp").resolve(strict=False)
    try:
        _PYTEST_DATA_DIR.relative_to(test_root)
    except ValueError:
        return
    shutil.rmtree(_PYTEST_DATA_DIR, ignore_errors=True)

# Configure pytest for async tests
pytest_plugins = ('pytest_asyncio',)


# P4-FIX: Removed session-scoped event_loop fixture - it conflicts with
# pytest-asyncio's default function-scoped loop and causes test isolation issues.
# Use pytest-asyncio's default loop per-test which is safer.


@pytest_asyncio.fixture
async def db_engine():
    """Create test database engine.

    P4-FIX: Changed from session-scope to function-scope to be compatible
    with pytest-asyncio's default event loop scope. Each test now gets a
    fresh in-memory database for full isolation.
    """
    from core.database import Base, db_manager

    test_database_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if test_database_url and not (
        os.getenv("ALLOW_EXTERNAL_TEST_DATABASE", "false").lower() == "true"
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL requires ALLOW_EXTERNAL_TEST_DATABASE=true "
            "to prevent accidental schema deletion"
        )

    # Default to isolated in-memory SQLite. CI also runs the live-pipeline
    # suite against an explicitly authorised ephemeral PostgreSQL service.
    engine = create_async_engine(
        test_database_url or "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Create partial index for payment tx_hash uniqueness (not created by metadata.create_all)
        from sqlalchemy import text
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_tx_hash_non_empty "
            "ON payments(tx_hash) WHERE tx_hash <> ''"
        ))

    # Set the global db_manager so app code uses the test database
    db_manager.engine = engine
    from sqlalchemy.orm import sessionmaker
    db_manager.async_session_factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine, cleanup_db):
    """Create test database session."""
    from core.database import db_manager

    async with db_manager.async_session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def cleanup_db(db_engine):
    """Clean the shared test database only for tests that actually use it."""

    yield

    # Clean all tables after test
    async with db_engine.begin() as conn:
        from core.database import Base
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest.fixture(autouse=True)
def isolate_global_settings():
    """Save and restore global settings between tests to prevent state leakage."""
    import copy

    from core.config import settings

    original_limit_timeout = copy.deepcopy(settings.exchange.limit_timeout_overrides)
    original_exchange_name = settings.exchange.name
    original_live_trading = settings.exchange.live_trading
    original_sandbox = settings.exchange.sandbox_mode

    yield

    settings.exchange.limit_timeout_overrides = original_limit_timeout
    settings.exchange.name = original_exchange_name
    settings.exchange.live_trading = original_live_trading
    settings.exchange.sandbox_mode = original_sandbox


@pytest.fixture(autouse=True)
def isolate_runtime_state_files(tmp_path, monkeypatch):
    """Give every test fresh persistent and in-memory trading safety state."""
    import position_monitor
    import pre_filter
    from core import account_risk, confidence_calibrator, portfolio_risk

    state_dir = tmp_path / "runtime_state"
    monkeypatch.setattr(account_risk, "_ACCOUNT_TRACKER_FILE", state_dir / "account_risk_tracker.json")
    monkeypatch.setattr(account_risk, "_DRAWDOWN_STATE_FILE", state_dir / "drawdown_cb_state.json")
    monkeypatch.setattr(position_monitor, "_GHOST_TRACKER_FILE", state_dir / "ghost_position_tracker.json")
    monkeypatch.setattr(pre_filter, "_STATS_FILE", state_dir / "filter_stats.json")
    monkeypatch.setattr(pre_filter, "_PERFORMANCE_FILE", state_dir / "filter_performance.json")
    monkeypatch.setattr(confidence_calibrator, "_CALIBRATION_FILE", state_dir / "ai_calibration.json")
    monkeypatch.setattr(portfolio_risk, "_VAR_CACHE_FILE", state_dir / "portfolio_var_cache.json")

    account_risk._ACCOUNT_DAILY_TRACKER.clear()
    account_risk._DRAWDOWN_CIRCUIT_BREAKERS.clear()
    account_risk._LIVE_EQUITY_CACHE.clear()
    position_monitor._GHOST_POSITION_TRACKER.clear()
    position_monitor._PROTECTIVE_ORDERS_LAST_VERIFY.clear()
    pre_filter._filter_stats.clear()
    pre_filter._filter_stats_buffer.clear()
    pre_filter._block_history.clear()
    pre_filter._CIRCUIT_BREAKERS.clear()
    pre_filter._pending_outcomes.clear()
    pre_filter._check_performance.clear()
    confidence_calibrator._CALIBRATION_CACHE.clear()
    portfolio_risk._VAR_CACHE.clear()

    yield

    account_risk._ACCOUNT_DAILY_TRACKER.clear()
    account_risk._DRAWDOWN_CIRCUIT_BREAKERS.clear()
    account_risk._LIVE_EQUITY_CACHE.clear()
    position_monitor._GHOST_POSITION_TRACKER.clear()
    position_monitor._PROTECTIVE_ORDERS_LAST_VERIFY.clear()
    pre_filter._filter_stats.clear()
    pre_filter._filter_stats_buffer.clear()
    pre_filter._block_history.clear()
    pre_filter._CIRCUIT_BREAKERS.clear()
    pre_filter._pending_outcomes.clear()
    pre_filter._check_performance.clear()
    confidence_calibrator._CALIBRATION_CACHE.clear()
    portfolio_risk._VAR_CACHE.clear()


@pytest.fixture(autouse=True)
def isolate_redis_coordination_client():
    """Give every test a fresh Redis coordination client.

    The cached client is bound to the event loop that created it. pytest-asyncio
    runs each test on a new loop, so a client cached by an earlier test would be
    reused on a closed loop and fail with "Event loop is closed" (this surfaces in
    the PostgreSQL+Redis CI job where REDIS_ENABLED=true).
    """
    import core.redis_coordination as redis_coordination

    redis_coordination._CLIENT = None
    redis_coordination._REDIS_UNAVAILABLE_LOGGED = False
    yield
    redis_coordination._CLIENT = None
    redis_coordination._REDIS_UNAVAILABLE_LOGGED = False


@pytest_asyncio.fixture
async def client(db_engine, cleanup_db):
    """Create test HTTP client."""
    from httpx import ASGITransport

    from core.factory import create_app

    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
def test_user_data():
    """Test user data fixture."""
    import secrets
    return {
        "username": "testuser",
        "email": "test@example.com",
        "password": f"T{secrets.token_urlsafe(16)}!1",
    }


@pytest.fixture
def test_admin_data():
    """Test admin data fixture."""
    import secrets
    return {
        "username": "admin",
        "email": "admin@example.com",
        "password": f"A{secrets.token_urlsafe(16)}!1",
    }


@pytest.fixture
def mock_exchange():
    """Mock exchange instance."""
    exchange = Mock()
    exchange.id = "binance"
    exchange.set_leverage = Mock(return_value={"leverage": 10})
    exchange.create_order = Mock(return_value={"id": "order123", "status": "closed"})
    exchange.fetch_order = Mock(return_value={"id": "order123", "status": "closed"})
    exchange.cancel_order = Mock(return_value={"id": "order123"})
    exchange.close = Mock()
    yield exchange


@pytest.fixture
def mock_ai_provider():
    """Mock AI provider response."""
    response = {
        "confidence": 0.85,
        "recommendation": "execute",
        "reasoning": "Strong bullish signal with good risk/reward",
        "suggested_direction": None,
        "suggested_entry": 50000.0,
        "suggested_stop_loss": 48000.0,
        "suggested_take_profit": 52000.0,
        "position_size_pct": 0.5,
        "recommended_leverage": 10,
        "risk_score": 0.4,
        "market_condition": "trending_up",
    }
    yield response


@pytest.fixture
def sample_signal():
    """Sample TradingView signal."""
    from models import SignalDirection, TradingViewSignal

    signal = TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        price=50000.0,
        timeframe="1h",
        strategy="test_strategy",
        message="Test signal",
    )
    yield signal


@pytest.fixture
def sample_market_context():
    """Sample market context."""
    from models import MarketContext

    market = MarketContext(
        ticker="BTCUSDT",
        current_price=50000.0,
        price_change_1h=2.5,
        price_change_4h=5.0,
        price_change_24h=10.0,
        volume_24h=1000000.0,
        high_24h=52000.0,
        low_24h=48000.0,
        bid_ask_spread=0.01,
        funding_rate=0.0001,
        rsi_1h=60.0,
        atr_pct=2.5,
    )
    yield market


@pytest.fixture
def sample_position():
    """Sample position model."""
    from core.database import PositionModel
    from core.utils.datetime import utcnow

    position = PositionModel(
        id="pos123",
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=50000.0,
        quantity=0.01,
        remaining_quantity=0.01,
        opened_at=utcnow(),
        leverage=10.0,
        margin=50.0,
        stop_loss=48000.0,
        take_profit_json='[{"price": 52000, "qty_pct": 100}]',
    )
    yield position


@pytest.fixture
def mock_redis_client():
    """Mock Redis client."""
    redis_client = Mock()
    redis_client.get = Mock(return_value=None)
    redis_client.set = Mock(return_value=True)
    redis_client.setex = Mock(return_value=True)
    redis_client.delete = Mock(return_value=1)
    redis_client.ping = Mock(return_value=True)
    redis_client.close = Mock()
    yield redis_client


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Temporary cache directory for tests."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    yield cache_dir


@pytest.fixture
def temp_log_dir(tmp_path):
    """Temporary log directory for tests."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    yield log_dir


# Coverage configuration
def pytest_configure(config):
    """Configure pytest with coverage settings."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "chaos: marks tests as chaos engineering tests"
    )


# Auto markers
def pytest_collection_modifyitems(config, items):
    """Auto-mark async tests."""
    for item in items:
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)

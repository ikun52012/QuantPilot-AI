"""
P4-FIX: QuantPilot Test Configuration
Pytest configuration with fixtures for unit and integration tests.
"""
import asyncio
from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Configure pytest for async tests
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def db_engine():
    """Create test database engine."""
    from core.database import Base, db_manager

    # Use in-memory SQLite for tests
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
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


@pytest.fixture
async def db_session(db_engine):
    """Create test database session."""
    from core.database import db_manager

    async with db_manager.async_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture(autouse=True)
async def cleanup_db(db_engine):
    """Clean up database after each test to ensure isolation."""

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


@pytest.fixture
async def client(db_engine):
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
    return {
        "username": "testuser",
        "email": "test@example.com",
        "password": "Str0ng!Pass#2024",
    }


@pytest.fixture
def test_admin_data():
    """Test admin data fixture."""
    return {
        "username": "admin",
        "email": "admin@example.com",
        "password": "AdminPass123!@#",
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
    from datetime import datetime

    from core.database import PositionModel

    position = PositionModel(
        id="pos123",
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=50000.0,
        quantity=0.01,
        remaining_quantity=0.01,
        opened_at=datetime.utcnow(),
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

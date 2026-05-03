"""
Signal Server - Database Layer (Enhanced)
Async SQLAlchemy with PostgreSQL/SQLite support.
"""
import hashlib
import json
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from core.config import settings
from core.utils.common import position_symbol_key, resolve_limit_timeout_secs
from core.utils.datetime import parse_datetime_utc_naive, utcnow


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class UserModel(Base):
    """User database model."""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(32), unique=True, nullable=False, index=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), default="user")
    balance_usdt = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: utcnow())
    last_login = Column(DateTime, nullable=True)
    settings_json = Column(Text, default="{}")
    webhook_secret = Column(String(128), default="")
    webhook_secret_hash = Column(String(64), default="", index=True)
    live_trading_allowed = Column(Boolean, default=False)
    max_leverage = Column(Integer, default=20)
    max_position_pct = Column(Float, default=10.0)
    token_version = Column(Integer, default=0)
    password_changed_at = Column(DateTime, nullable=True)
    # Soft delete support
    deleted_at = Column(DateTime, nullable=True, default=None, index=True)

    # 2FA fields
    totp_secret = Column(String(256), default="", doc="Encrypted TOTP secret")
    totp_enabled = Column(Boolean, default=False)
    totp_recovery_codes_json = Column(Text, default="[]", doc="JSON array of hashed recovery codes")

    # Relationships
    subscriptions = relationship("SubscriptionModel", back_populates="user", lazy="dynamic")
    payments = relationship("PaymentModel", back_populates="user", lazy="dynamic")
    trades = relationship("TradeModel", back_populates="user", lazy="dynamic")


class SubscriptionPlanModel(Base):
    """Subscription plan model."""
    __tablename__ = "subscription_plans"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    price_usdt = Column(Float, nullable=False)
    duration_days = Column(Integer, nullable=False)
    features_json = Column(Text, default="[]")
    is_active = Column(Boolean, default=True)
    max_signals_per_day = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: utcnow())

    subscriptions = relationship("SubscriptionModel", back_populates="plan", lazy="dynamic")


class SubscriptionModel(Base):
    """User subscription model."""
    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(String(36), ForeignKey("subscription_plans.id"), nullable=False)
    status = Column(String(20), default="pending", index=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: utcnow())

    user = relationship("UserModel", back_populates="subscriptions")
    plan = relationship("SubscriptionPlanModel", back_populates="subscriptions")


class PaymentModel(Base):
    """Payment model."""
    __tablename__ = "payments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = Column(String(36), ForeignKey("subscriptions.id"), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(12), default="USDT")
    network = Column(String(20), default="TRC20")
    tx_hash = Column(String(200), default="")
    wallet_address = Column(String(128), default="")
    status = Column(String(20), default="pending", index=True)
    created_at = Column(DateTime, default=lambda: utcnow())
    confirmed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("UserModel", back_populates="payments")


class TradeModel(Base):
    """Trade log model."""
    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_user_timestamp", "user_id", "timestamp"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    ticker = Column(String(40), default="")
    direction = Column(String(20), default="")
    execute = Column(Boolean, default=False)
    order_status = Column(String(20), default="")
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    pnl_usdt = Column(Float, default=0.0)
    fees_usdt = Column(Float, default=0.0)
    fees_pct = Column(Float, default=0.0)
    execution_latency_ms = Column(Integer, default=0)
    strategy_name = Column(String(120), default="")
    signal_source = Column(String(20), default="tradingview")
    payload_json = Column(Text, nullable=False)

    user = relationship("UserModel", back_populates="trades")


class WebhookEventModel(Base):
    """Webhook event log model."""
    __tablename__ = "webhook_events"
    __table_args__ = (
        Index("idx_webhook_fingerprint_created", "fingerprint", "created_at", unique=False),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=True, index=True)
    fingerprint = Column(String(64), nullable=False, index=True)
    ticker = Column(String(40), default="")
    direction = Column(String(20), default="")
    status = Column(String(20), nullable=False)
    status_code = Column(Integer, default=200)
    reason = Column(Text, default="")
    client_ip = Column(String(45), default="")
    payload_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: utcnow(), index=True)


class AdminSettingModel(Base):
    """Admin settings model."""
    __tablename__ = "admin_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: utcnow())


class AdminAuditLogModel(Base):
    """Admin audit log model."""
    __tablename__ = "admin_audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    admin_id = Column(String(36), nullable=True)
    admin_username = Column(String(32), default="")
    action = Column(String(100), nullable=False)
    target_type = Column(String(50), default="")
    target_id = Column(String(36), default="")
    summary = Column(Text, default="")
    client_ip = Column(String(45), default="")
    created_at = Column(DateTime, default=lambda: utcnow(), index=True)


class InviteCodeModel(Base):
    """Invite code model."""
    __tablename__ = "invite_codes"

    code = Column(String(80), primary_key=True)
    note = Column(Text, default="")
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=lambda: utcnow())
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    last_used_by = Column(String(36), nullable=True)
    last_used_at = Column(DateTime, nullable=True)


class RedeemCodeModel(Base):
    """Redeem code model."""
    __tablename__ = "redeem_codes"

    code = Column(String(80), primary_key=True)
    plan_id = Column(String(36), ForeignKey("subscription_plans.id"), nullable=True)
    duration_days = Column(Integer, default=0)
    balance_usdt = Column(Float, default=0.0)
    note = Column(Text, default="")
    is_active = Column(Boolean, default=True, index=True)
    redeemed_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    redeemed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: utcnow())
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)


class PositionModel(Base):
    """Position tracking model."""
    __tablename__ = "positions"
    __table_args__ = (
        Index("idx_positions_user_status", "user_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=True, index=True)
    ticker = Column(String(40), nullable=False, index=True)
    direction = Column(String(20), nullable=False)
    status = Column(String(20), default="open", index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, default=0.0)
    remaining_quantity = Column(Float, default=0.0)
    opened_at = Column(DateTime, nullable=False)
    open_trade_id = Column(String(36), nullable=True)
    entry_order_id = Column(String(128), default="")
    order_type = Column(String(40), default="market")
    limit_timeout_secs = Column(Float, default=14400.0)
    stop_loss = Column(Float, nullable=True)
    take_profit_json = Column(Text, default="[]")
    stop_loss_order_id = Column(String(128), default="")
    take_profit_order_ids_json = Column(Text, default="[]")
    trailing_stop_config_json = Column(Text, default="{}")
    exchange = Column(String(40), default="")
    live_trading = Column(Boolean, default=False)
    sandbox_mode = Column(Boolean, default=False)
    leverage = Column(Float, default=1.0)
    margin = Column(Float, default=0.0)
    liquidation_price = Column(Float, nullable=True)
    strategy_name = Column(String(120), default="")
    user_risk_profile = Column(String(20), default="balanced")
    realized_pnl_pct = Column(Float, default=0.0)
    current_pnl_pct = Column(Float, default=0.0)
    unrealized_pnl_usdt = Column(Float, default=0.0)
    fees_total_usdt = Column(Float, default=0.0)
    last_price = Column(Float, nullable=True)
    close_reason = Column(String(80), default="")
    updated_at = Column(DateTime, default=lambda: utcnow())
    exit_price = Column(Float, nullable=True)
    pnl_pct = Column(Float, default=0.0)
    closed_at = Column(DateTime, nullable=True)
    close_trade_id = Column(String(36), nullable=True)


class OrderEventModel(Base):
    """Order execution event ledger for reconciliation and retry review."""
    __tablename__ = "order_events"
    __table_args__ = (
        Index("idx_order_events_status_retry", "status", "retry_state", "next_retry_at"),
        Index("idx_order_events_user_created", "user_id", "created_at"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=True, index=True)
    position_id = Column(String(36), nullable=True, index=True)
    trade_id = Column(String(36), nullable=True, index=True)
    client_order_id = Column(String(128), default="", index=True)
    exchange_order_id = Column(String(128), default="", index=True)
    ticker = Column(String(40), default="", index=True)
    direction = Column(String(20), default="")
    order_type = Column(String(40), default="")
    status = Column(String(30), default="created", index=True)
    retry_state = Column(String(30), default="not_required", index=True)
    attempt_count = Column(Integer, default=0)
    last_error = Column(Text, default="")
    payload_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: utcnow(), index=True)
    updated_at = Column(DateTime, default=lambda: utcnow())
    next_retry_at = Column(DateTime, nullable=True)


class StrategyStateModel(Base):
    """Persistent runtime state for DCA, grid, and custom strategies."""
    __tablename__ = "strategy_states"
    __table_args__ = (
        Index("idx_strategy_states_user_type", "user_id", "strategy_type"),
        Index("idx_strategy_states_type_status", "strategy_type", "status"),
    )

    id = Column(String(120), primary_key=True)
    user_id = Column(String(36), nullable=True, index=True)
    strategy_type = Column(String(32), nullable=False, index=True)
    ticker = Column(String(40), default="", index=True)
    name = Column(String(120), default="")
    status = Column(String(30), default="active", index=True)
    config_json = Column(Text, default="{}")
    state_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: utcnow())
    updated_at = Column(DateTime, default=lambda: utcnow())


class SharedSignalModel(Base):
    """Community signal shared by a user."""
    __tablename__ = "shared_signals"
    __table_args__ = (
        Index("idx_shared_signals_status_created", "status", "created_at"),
        Index("idx_shared_signals_ticker_direction", "ticker", "direction"),
    )

    id = Column(String(80), primary_key=True)
    user_id = Column(String(36), nullable=True, index=True)
    username = Column(String(64), default="")
    ticker = Column(String(40), default="", index=True)
    direction = Column(String(20), default="", index=True)
    entry_price = Column(Float, default=0.0)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    confidence = Column(Float, default=0.0)
    strategy_name = Column(String(120), default="")
    reason = Column(Text, default="")
    status = Column(String(20), default="active", index=True)
    is_private = Column(Boolean, default=False)
    subscribers_count = Column(Integer, default=0)
    executions_count = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    stats_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=lambda: utcnow(), index=True)
    updated_at = Column(DateTime, default=lambda: utcnow())


class SignalSubscriptionModel(Base):
    """User subscription to a shared community signal."""
    __tablename__ = "signal_subscriptions"
    __table_args__ = (
        Index("idx_signal_subscriptions_user", "user_id"),
        Index("idx_signal_subscriptions_signal", "signal_id"),
    )

    id = Column(String(120), primary_key=True)
    user_id = Column(String(36), nullable=True, index=True)
    signal_id = Column(String(80), ForeignKey("shared_signals.id"), nullable=False, index=True)
    auto_execute = Column(Boolean, default=False)
    max_position_pct = Column(Float, default=10.0)
    created_at = Column(DateTime, default=lambda: utcnow())


# ─────────────────────────────────────────────
# Database Engine & Session
# ─────────────────────────────────────────────

class DatabaseManager:
    """Async database manager."""

    def __init__(self):
        self.engine = None
        self.async_session_factory = None

    async def init(self):
        """Initialize database engine and create tables."""
        db_url = settings.database.url
        is_sqlite = "sqlite" in db_url

        # Ensure data directory exists for SQLite
        if is_sqlite:
            db_path = db_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        engine_kwargs = {"echo": settings.database.echo}
        if "postgresql" in db_url:
            engine_kwargs.update(
                pool_size=settings.database.pool_size,
                max_overflow=settings.database.max_overflow,
            )
        elif is_sqlite:
            engine_kwargs["connect_args"] = {"timeout": 30}

        self.engine = create_async_engine(db_url, **engine_kwargs)

        if is_sqlite:
            @event.listens_for(self.engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.async_session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._ensure_schema)

        logger.info(f"[Database] Initialized: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    @staticmethod
    def _ensure_schema(sync_conn):
        """Apply lightweight additive migrations for deployments without Alembic.

        DDL types used are ANSI SQL standard and compatible with SQLite, PostgreSQL,
        and MySQL. For PostgreSQL production deployments, Alembic migrations are
        still recommended for complex schema changes.
        """
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
        dialect_name = sync_conn.dialect.name
        is_postgresql = dialect_name == "postgresql"

        if is_postgresql:
            logger.info(
                "[Database] PostgreSQL detected — _ensure_schema uses ANSI-compatible DDL. "
                "Use Alembic for complex schema migrations."
            )

        VALID_TABLES = {
            "users", "subscription_plans", "subscriptions", "payments",
            "trades", "webhook_events", "invite_codes", "redeem_codes",
            "admin_settings", "admin_audit_logs", "positions",
            "order_events", "strategy_states", "shared_signals", "signal_subscriptions",
        }
        VALID_COLUMN_TYPES = {
            "FLOAT", "BOOLEAN", "TIMESTAMP", "TEXT", "VARCHAR", "INTEGER",
            "DOUBLE PRECISION", "REAL",
        }

        # Map ANSI types to dialect-specific equivalents
        if is_postgresql:
            _type_map = {
                "FLOAT": "DOUBLE PRECISION",
                "BOOLEAN": "BOOLEAN",
                "TIMESTAMP": "TIMESTAMP",
                "VARCHAR": "VARCHAR",
                "INTEGER": "INTEGER",
                "TEXT": "TEXT",
            }
        else:
            _type_map = None

        def validate_identifier(name: str) -> bool:
            return bool(re.match(r'^[a-z_][a-z0-9_]*$', name))

        def validate_ddl(ddl: str) -> bool:
            upper_ddl = ddl.upper()
            return any(t in upper_ddl for t in VALID_COLUMN_TYPES)

        def add_missing_columns(table_name: str, columns: dict[str, str]) -> None:
            if table_name not in tables or table_name not in VALID_TABLES:
                return
            if not validate_identifier(table_name):
                logger.warning(f"[Database] Invalid table name: {table_name}")
                return
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for name, ddl in columns.items():
                if name not in existing:
                    if not validate_identifier(name):
                        logger.warning(f"[Database] Invalid column name: {name}")
                        continue
                    if not validate_ddl(ddl):
                        logger.warning(f"[Database] Invalid DDL for column {name}: {ddl}")
                        continue
                    # Translate type for dialect if needed
                    effective_ddl = ddl
                    if _type_map:
                        upper_prefix = ddl.split()[0].upper() if ddl.strip() else ""
                        if upper_prefix in _type_map and _type_map[upper_prefix] != upper_prefix:
                            effective_ddl = ddl.replace(upper_prefix, _type_map[upper_prefix], 1)
                    quoted_table = sync_conn.dialect.identifier_preparer.quote(table_name)
                    quoted_column = sync_conn.dialect.identifier_preparer.quote(name)
                    sync_conn.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {effective_ddl}"))

        def create_index_if_missing(name: str, ddl: str) -> None:
            existing_indexes = {
                index["name"]
                for table_name in tables
                for index in inspector.get_indexes(table_name)
            }
            if name not in existing_indexes:
                sync_conn.execute(text(ddl))

        add_missing_columns("users", {
            "balance_usdt": "FLOAT DEFAULT 0",
            "is_active": "BOOLEAN DEFAULT true",
            "created_at": "TIMESTAMP",
            "last_login": "TIMESTAMP",
            "settings_json": "TEXT DEFAULT '{}'",
            "webhook_secret": "VARCHAR(128) DEFAULT ''",
            "webhook_secret_hash": "VARCHAR(64) DEFAULT ''",
            "live_trading_allowed": "BOOLEAN DEFAULT false",
            "max_leverage": "INTEGER DEFAULT 20",
            "max_position_pct": "FLOAT DEFAULT 10",
            "token_version": "INTEGER DEFAULT 0",
            "password_changed_at": "TIMESTAMP",
            "totp_secret": "VARCHAR(256) DEFAULT ''",
            "totp_enabled": "BOOLEAN DEFAULT false",
            "totp_recovery_codes_json": "TEXT DEFAULT '[]'",
            "deleted_at": "TIMESTAMP",
        })
        add_missing_columns("subscription_plans", {
            "description": "TEXT DEFAULT ''",
            "features_json": "TEXT DEFAULT '[]'",
            "is_active": "BOOLEAN DEFAULT true",
            "max_signals_per_day": "INTEGER DEFAULT 0",
            "created_at": "TIMESTAMP",
        })
        add_missing_columns("subscriptions", {
            "status": "VARCHAR(20) DEFAULT 'pending'",
            "start_date": "TIMESTAMP",
            "end_date": "TIMESTAMP",
            "created_at": "TIMESTAMP",
        })
        add_missing_columns("payments", {
            "subscription_id": "VARCHAR(36)",
            "currency": "VARCHAR(12) DEFAULT 'USDT'",
            "network": "VARCHAR(20) DEFAULT 'TRC20'",
            "tx_hash": "VARCHAR(200) DEFAULT ''",
            "wallet_address": "VARCHAR(128) DEFAULT ''",
            "status": "VARCHAR(20) DEFAULT 'pending'",
            "created_at": "TIMESTAMP",
            "confirmed_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
        })
        add_missing_columns("trades", {
            "user_id": "VARCHAR(36)",
            "timestamp": "TIMESTAMP",
            "ticker": "VARCHAR(40) DEFAULT ''",
            "direction": "VARCHAR(20) DEFAULT ''",
            "execute": "BOOLEAN DEFAULT false",
            "order_status": "VARCHAR(20) DEFAULT ''",
            "entry_price": "FLOAT",
            "exit_price": "FLOAT",
            "quantity": "FLOAT DEFAULT 0",
            "pnl_pct": "FLOAT DEFAULT 0",
            "pnl_usdt": "FLOAT DEFAULT 0",
            "fees_usdt": "FLOAT DEFAULT 0",
            "fees_pct": "FLOAT DEFAULT 0",
            "execution_latency_ms": "INTEGER DEFAULT 0",
            "strategy_name": "VARCHAR(120) DEFAULT ''",
            "signal_source": "VARCHAR(20) DEFAULT 'tradingview'",
            "payload_json": "TEXT DEFAULT '{}'",
        })
        add_missing_columns("webhook_events", {
            "user_id": "VARCHAR(36)",
            "fingerprint": "VARCHAR(64) DEFAULT ''",
            "ticker": "VARCHAR(40) DEFAULT ''",
            "direction": "VARCHAR(20) DEFAULT ''",
            "status_code": "INTEGER DEFAULT 200",
            "reason": "TEXT DEFAULT ''",
            "client_ip": "VARCHAR(45) DEFAULT ''",
            "payload_json": "TEXT DEFAULT '{}'",
            "created_at": "TIMESTAMP",
        })
        add_missing_columns("invite_codes", {
            "note": "TEXT DEFAULT ''",
            "max_uses": "INTEGER DEFAULT 1",
            "used_count": "INTEGER DEFAULT 0",
            "is_active": "BOOLEAN DEFAULT true",
            "created_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
            "created_by": "VARCHAR(36)",
            "last_used_by": "VARCHAR(36)",
            "last_used_at": "TIMESTAMP",
        })
        add_missing_columns("redeem_codes", {
            "plan_id": "VARCHAR(36)",
            "duration_days": "INTEGER DEFAULT 0",
            "balance_usdt": "FLOAT DEFAULT 0",
            "note": "TEXT DEFAULT ''",
            "is_active": "BOOLEAN DEFAULT true",
            "redeemed_by": "VARCHAR(36)",
            "redeemed_at": "TIMESTAMP",
            "created_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
            "created_by": "VARCHAR(36)",
        })
        add_missing_columns("positions", {
            "remaining_quantity": "FLOAT DEFAULT 0",
            "open_trade_id": "VARCHAR(36)",
            "entry_order_id": "VARCHAR(128) DEFAULT ''",
            "order_type": "VARCHAR(40) DEFAULT 'market'",
            "limit_timeout_secs": "FLOAT DEFAULT 14400",
            "stop_loss": "FLOAT",
            "take_profit_json": "TEXT DEFAULT '[]'",
            "stop_loss_order_id": "VARCHAR(128) DEFAULT ''",
            "take_profit_order_ids_json": "TEXT DEFAULT '[]'",
            "trailing_stop_config_json": "TEXT DEFAULT '{}'",
            "exchange": "VARCHAR(40) DEFAULT ''",
            "live_trading": "BOOLEAN DEFAULT false",
            "sandbox_mode": "BOOLEAN DEFAULT false",
            "leverage": "FLOAT DEFAULT 1",
            "margin": "FLOAT DEFAULT 0",
            "liquidation_price": "FLOAT",
            "strategy_name": "VARCHAR(120) DEFAULT ''",
            "user_risk_profile": "VARCHAR(20) DEFAULT 'balanced'",
            "realized_pnl_pct": "FLOAT DEFAULT 0",
            "current_pnl_pct": "FLOAT DEFAULT 0",
            "unrealized_pnl_usdt": "FLOAT DEFAULT 0",
            "fees_total_usdt": "FLOAT DEFAULT 0",
            "last_price": "FLOAT",
            "close_reason": "VARCHAR(80) DEFAULT ''",
            "updated_at": "TIMESTAMP",
            "exit_price": "FLOAT",
            "pnl_pct": "FLOAT DEFAULT 0",
            "closed_at": "TIMESTAMP",
            "close_trade_id": "VARCHAR(36)",
        })
        add_missing_columns("order_events", {
            "user_id": "VARCHAR(36)",
            "position_id": "VARCHAR(36)",
            "trade_id": "VARCHAR(36)",
            "client_order_id": "VARCHAR(128) DEFAULT ''",
            "exchange_order_id": "VARCHAR(128) DEFAULT ''",
            "ticker": "VARCHAR(40) DEFAULT ''",
            "direction": "VARCHAR(20) DEFAULT ''",
            "order_type": "VARCHAR(40) DEFAULT ''",
            "status": "VARCHAR(30) DEFAULT 'created'",
            "retry_state": "VARCHAR(30) DEFAULT 'not_required'",
            "attempt_count": "INTEGER DEFAULT 0",
            "last_error": "TEXT DEFAULT ''",
            "payload_json": "TEXT DEFAULT '{}'",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
            "next_retry_at": "TIMESTAMP",
        })
        add_missing_columns("strategy_states", {
            "user_id": "VARCHAR(36)",
            "strategy_type": "VARCHAR(32) DEFAULT ''",
            "ticker": "VARCHAR(40) DEFAULT ''",
            "name": "VARCHAR(120) DEFAULT ''",
            "status": "VARCHAR(30) DEFAULT 'active'",
            "config_json": "TEXT DEFAULT '{}'",
            "state_json": "TEXT DEFAULT '{}'",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
        })
        add_missing_columns("shared_signals", {
            "user_id": "VARCHAR(36)",
            "username": "VARCHAR(64) DEFAULT ''",
            "ticker": "VARCHAR(40) DEFAULT ''",
            "direction": "VARCHAR(20) DEFAULT ''",
            "entry_price": "FLOAT DEFAULT 0",
            "stop_loss": "FLOAT",
            "take_profit": "FLOAT",
            "confidence": "FLOAT DEFAULT 0",
            "strategy_name": "VARCHAR(120) DEFAULT ''",
            "reason": "TEXT DEFAULT ''",
            "status": "VARCHAR(20) DEFAULT 'active'",
            "is_private": "BOOLEAN DEFAULT false",
            "subscribers_count": "INTEGER DEFAULT 0",
            "executions_count": "INTEGER DEFAULT 0",
            "success_rate": "FLOAT DEFAULT 0",
            "stats_json": "TEXT DEFAULT '{}'",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
        })
        add_missing_columns("signal_subscriptions", {
            "user_id": "VARCHAR(36)",
            "signal_id": "VARCHAR(80) DEFAULT ''",
            "auto_execute": "BOOLEAN DEFAULT false",
            "max_position_pct": "FLOAT DEFAULT 10",
            "created_at": "TIMESTAMP",
        })
        create_index_if_missing("idx_users_webhook_secret_hash", "CREATE INDEX idx_users_webhook_secret_hash ON users(webhook_secret_hash)")
        create_index_if_missing("idx_trades_user_timestamp", "CREATE INDEX idx_trades_user_timestamp ON trades(user_id, timestamp)")
        create_index_if_missing("idx_webhook_fingerprint_created", "CREATE INDEX idx_webhook_fingerprint_created ON webhook_events(fingerprint, created_at)")
        create_index_if_missing("idx_positions_status", "CREATE INDEX idx_positions_status ON positions(status)")
        create_index_if_missing("idx_positions_user_status", "CREATE INDEX idx_positions_user_status ON positions(user_id, status)")
        create_index_if_missing("idx_order_events_status_retry", "CREATE INDEX idx_order_events_status_retry ON order_events(status, retry_state, next_retry_at)")
        create_index_if_missing("idx_order_events_user_created", "CREATE INDEX idx_order_events_user_created ON order_events(user_id, created_at)")
        create_index_if_missing("idx_strategy_states_user_type", "CREATE INDEX idx_strategy_states_user_type ON strategy_states(user_id, strategy_type)")
        create_index_if_missing("idx_strategy_states_type_status", "CREATE INDEX idx_strategy_states_type_status ON strategy_states(strategy_type, status)")
        create_index_if_missing("idx_shared_signals_status_created", "CREATE INDEX idx_shared_signals_status_created ON shared_signals(status, created_at)")
        create_index_if_missing("idx_shared_signals_ticker_direction", "CREATE INDEX idx_shared_signals_ticker_direction ON shared_signals(ticker, direction)")
        create_index_if_missing("idx_signal_subscriptions_user", "CREATE INDEX idx_signal_subscriptions_user ON signal_subscriptions(user_id)")
        create_index_if_missing("idx_signal_subscriptions_signal", "CREATE INDEX idx_signal_subscriptions_signal ON signal_subscriptions(signal_id)")
        if dialect_name in {"sqlite", "postgresql"}:
            create_index_if_missing(
                "uq_payments_tx_hash_non_empty",
                "CREATE UNIQUE INDEX uq_payments_tx_hash_non_empty ON payments(tx_hash) WHERE tx_hash <> ''",
            )

    async def close(self):
        """Close database connections."""
        if self.engine:
            await self.engine.dispose()

    async def get_session(self) -> AsyncSession:
        """Get a new database session."""
        return self.async_session_factory()


# Global database manager
db_manager = DatabaseManager()


async def get_db() -> AsyncSession:
    """FastAPI dependency for database session."""
    async with db_manager.async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

def _webhook_secret_hash(secret: str) -> str:
    """Hash webhook secret for secure storage."""
    secret = str(secret or "").strip()
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────
# User CRUD
# ─────────────────────────────────────────────

async def create_user(
    session: AsyncSession,
    username: str,
    email: str,
    password_hash: str,
    role: str = "user"
) -> UserModel:
    """Create a new user."""
    user = UserModel(
        username=username.lower().strip(),
        email=email.lower().strip(),
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user_by_username(session: AsyncSession, username: str) -> UserModel | None:
    """Get user by username."""
    result = await session.execute(
        select(UserModel).where(UserModel.username == username.lower().strip())
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: str) -> UserModel | None:
    """Get user by ID."""
    result = await session.execute(
        select(UserModel).where(UserModel.id == user_id)
    )
    return result.scalar_one_or_none()


async def lock_user_by_id(session: AsyncSession, user_id: str) -> UserModel | None:
    """Get and lock a user row for transactional updates."""
    result = await session.execute(
        select(UserModel).where(UserModel.id == user_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> UserModel | None:
    """Get user by email."""
    result = await session.execute(
        select(UserModel).where(UserModel.email == email.lower().strip())
    )
    return result.scalar_one_or_none()


async def update_user_login(session: AsyncSession, user_id: str):
    """Update user's last login time."""
    await session.execute(
        update(UserModel).where(UserModel.id == user_id).values(last_login=utcnow())
    )


async def update_user_status(session: AsyncSession, user_id: str, is_active: bool):
    """Update user active status."""
    await session.execute(
        update(UserModel)
        .where(UserModel.id == user_id)
        .values(is_active=is_active, token_version=UserModel.token_version + 1)
    )


async def update_user_password_hash(session: AsyncSession, user_id: str, password_hash: str) -> bool:
    """Update user password hash."""
    result = await session.execute(
        update(UserModel)
        .where(UserModel.id == user_id)
        .values(password_hash=password_hash, password_changed_at=utcnow(), token_version=UserModel.token_version + 1)
    )
    return result.rowcount > 0


async def soft_delete_user(session: AsyncSession, user_id: str) -> bool:
    """Soft delete a user by setting deleted_at timestamp."""
    result = await session.execute(
        update(UserModel)
        .where(UserModel.id == user_id, UserModel.deleted_at.is_(None))
        .values(deleted_at=utcnow(), is_active=False, token_version=UserModel.token_version + 1)
    )
    return result.rowcount > 0


async def restore_user(session: AsyncSession, user_id: str) -> bool:
    """Restore a soft-deleted user."""
    result = await session.execute(
        update(UserModel)
        .where(UserModel.id == user_id, UserModel.deleted_at.isnot(None))
        .values(deleted_at=None, is_active=True, token_version=UserModel.token_version + 1)
    )
    return result.rowcount > 0


async def get_all_users(session: AsyncSession, include_deleted: bool = False) -> list[UserModel]:
    """Get all users, optionally including soft-deleted ones."""
    query = select(UserModel).order_by(UserModel.created_at.desc())
    if not include_deleted:
        query = query.where(UserModel.deleted_at.is_(None))
    result = await session.execute(query)
    return list(result.scalars().all())


# ─────────────────────────────────────────────
# Subscription CRUD
# ─────────────────────────────────────────────

async def get_subscription_plans(session: AsyncSession, active_only: bool = True) -> list[SubscriptionPlanModel]:
    """Get all subscription plans."""
    query = select(SubscriptionPlanModel)
    if active_only:
        query = query.where(SubscriptionPlanModel.is_active)
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_user_active_subscription(session: AsyncSession, user_id: str) -> SubscriptionModel | None:
    """Get user's active subscription."""
    now = utcnow()
    result = await session.execute(
        select(SubscriptionModel)
        .where(
            SubscriptionModel.user_id == user_id,
            SubscriptionModel.status == "active",
            SubscriptionModel.end_date >= now,
        )
        .order_by(SubscriptionModel.end_date.desc())
    )
    return result.scalars().first()


async def deactivate_user_subscriptions(
    session: AsyncSession,
    user_id: str,
    *,
    exclude_subscription_id: str | None = None,
    reason_status: str = "cancelled",
) -> None:
    """Ensure a user has at most one active subscription row."""
    query = (
        update(SubscriptionModel)
        .where(SubscriptionModel.user_id == user_id, SubscriptionModel.status == "active")
        .values(status=reason_status)
    )
    if exclude_subscription_id:
        query = query.where(SubscriptionModel.id != exclude_subscription_id)
    await session.execute(query)


# ─────────────────────────────────────────────
# Trade CRUD
# ─────────────────────────────────────────────

async def log_trade_db(
    session: AsyncSession,
    user_id: str | None,
    ticker: str,
    direction: str,
    execute: bool,
    order_status: str,
    pnl_pct: float,
    payload: dict,
) -> TradeModel:
    """Log a trade to the database and keep the position ledger in sync."""
    trade_id = str(uuid.uuid4())
    timestamp = utcnow()
    payload = dict(payload or {})
    entry = {
        "id": trade_id,
        "user_id": user_id,
        "timestamp": timestamp.isoformat(),
        "ticker": ticker,
        "direction": direction,
        "execute": execute,
        "order_status": order_status,
        "pnl_pct": pnl_pct,
        "signal": payload.get("signal") or {},
        "analysis": payload.get("analysis") or {},
        "order_details": payload.get("result") or payload.get("order_details") or {},
        "exchange_config": payload.get("exchange_config") or payload.get("exchange") or {},
        "strategy_name": payload.get("strategy_name") or (payload.get("signal") or {}).get("strategy", ""),
        "user_risk_profile": payload.get("user_risk_profile") or "balanced",
    }

    entry = await sync_position_from_trade_entry_async(session, entry)
    payload.update({
        "position_id": entry.get("position_id"),
        "position_event": entry.get("position_event"),
        "close_reason": entry.get("close_reason"),
        "pnl_pct": float(entry.get("pnl_pct") or 0.0),
    })

    trade = TradeModel(
        id=trade_id,
        user_id=user_id,
        timestamp=timestamp,
        ticker=ticker,
        direction=direction,
        execute=execute,
        order_status=entry.get("order_status") or order_status,
        pnl_pct=float(entry.get("pnl_pct") or 0.0),
        payload_json=json.dumps(payload, default=str),
    )
    session.add(trade)
    await session.flush()
    return trade


async def count_today_executed_trades(session: AsyncSession, user_id: str | None = None) -> int:
    """Count today's executed trades."""
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    query = select(TradeModel).where(
        TradeModel.timestamp >= today_start,
        TradeModel.execute,
    )
    if user_id:
        query = query.where(TradeModel.user_id == user_id)
    result = await session.execute(query)
    return len(list(result.scalars().all()))


# ─────────────────────────────────────────────
# Trade Log CRUD (for trade_logger.py)
# ─────────────────────────────────────────────

async def insert_trade_log_async(session: AsyncSession, entry: dict) -> dict:
    """
    Insert a trade log entry into the database.
    Also syncs position tracking for PnL calculation.
    Returns the enriched entry.
    """
    entry = await sync_position_from_trade_entry_async(session, entry)

    trade = TradeModel(
        id=entry.get("id") or str(uuid.uuid4()),
        user_id=entry.get("user_id"),
        timestamp=_db_datetime(entry.get("timestamp")),
        ticker=entry.get("ticker", ""),
        direction=entry.get("direction", ""),
        execute=bool(entry.get("execute")),
        order_status=entry.get("order_status", ""),
        pnl_pct=float(entry.get("pnl_pct") or 0.0),
        payload_json=json.dumps(entry, ensure_ascii=False, default=str),
    )
    session.add(trade)
    await session.flush()
    return entry


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _db_datetime(value: Any) -> datetime:
    """Normalize timestamp input for timezone-neutral DB columns."""
    try:
        return parse_datetime_utc_naive(value)
    except (TypeError, ValueError):
        return utcnow()


def _loads_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _has_partial_position_fills(position: PositionModel) -> bool:
    levels = _loads_list(position.take_profit_json)
    return any(
        str(level.get("status") or "").lower() in {"hit", "filled", "closed"}
        for level in levels
        if isinstance(level, dict)
    )


def _effective_remaining_quantity(position: PositionModel, opened_qty: float) -> float:
    remaining_qty = _safe_float(position.remaining_quantity, opened_qty)
    if remaining_qty > 0:
        return remaining_qty

    # Older deployments may have open positions created before
    # remaining_quantity existed. Treat those as fully open only if no partial
    # TP has ever been recorded; otherwise zero really means fully settled.
    if (
        position.status in {"open", "pending"}
        and _safe_float(position.realized_pnl_pct) == 0
        and not _has_partial_position_fills(position)
    ):
        return opened_qty
    return 0.0


def _take_profit_levels_from_entry(entry: dict) -> list[dict]:
    order_details = entry.get("order_details") or {}
    analysis = entry.get("analysis") or {}
    raw_levels = order_details.get("take_profit_orders") or []
    levels = []

    for idx, level in enumerate(raw_levels, start=1):
        price = _safe_float(level.get("price"))
        if price <= 0:
            continue
        levels.append({
            "level": int(level.get("level") or idx),
            "price": price,
            "qty_pct": _safe_float(level.get("qty_pct"), 100.0),
            "order_id": str(level.get("order_id") or ""),
            "status": level.get("status") if level.get("status") != "simulated" else "pending",
        })

    if not levels:
        for idx in range(1, 5):
            price = _safe_float(analysis.get(f"suggested_tp{idx}"))
            if price <= 0:
                continue
            levels.append({
                "level": idx,
                "price": price,
                "qty_pct": _safe_float(analysis.get(f"tp{idx}_qty_pct"), 25.0),
                "order_id": "",
                "status": "pending",
            })

    total_qty = sum(max(0.0, _safe_float(level.get("qty_pct"))) for level in levels)
    if levels and total_qty <= 0:
        levels[0]["qty_pct"] = 100.0

    return levels


def _position_pnl_pct(direction: str, entry_price: float, exit_price: float, leverage: float = 1.0) -> float:
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    if direction == "short":
        raw = ((entry_price - exit_price) / entry_price) * 100.0
    else:
        raw = ((exit_price - entry_price) / entry_price) * 100.0
    return raw * max(1.0, _safe_float(leverage, 1.0))


async def close_position_async(
    session: AsyncSession,
    position: PositionModel,
    exit_price: float,
    close_reason: str,
    close_trade_id: str | None = None,
    closed_at: datetime | None = None,
) -> float:
    """Close a tracked position and return realised leveraged PnL percentage."""
    exit_price = _safe_float(exit_price)
    opened_qty = max(_safe_float(position.quantity), 0.0)
    remaining_qty = _effective_remaining_quantity(position, opened_qty)
    remaining_weight = min(1.0, max(0.0, remaining_qty / opened_qty)) if opened_qty > 0 else 1.0
    remaining_pnl = _position_pnl_pct(
        str(position.direction or "long").lower(),
        _safe_float(position.entry_price),
        exit_price,
        _safe_float(position.leverage, 1.0),
    ) * remaining_weight
    pnl_pct = round(_safe_float(position.realized_pnl_pct) + remaining_pnl, 6)

    # Calculate actual USDT PnL for balance update
    entry_price = _safe_float(position.entry_price)
    leverage = _safe_float(position.leverage, 1.0)
    if entry_price > 0 and opened_qty > 0:
        # Margin used = (entry_price * quantity) / leverage
        margin_used = (entry_price * opened_qty) / max(1.0, leverage)
        # PnL in USDT = margin_used * (pnl_pct / 100) * remaining_weight
        pnl_usdt = margin_used * (remaining_pnl / 100.0)
    else:
        pnl_usdt = 0.0

    now = closed_at or utcnow()
    position.status = "closed"
    position.exit_price = exit_price
    position.pnl_pct = pnl_pct
    position.current_pnl_pct = pnl_pct
    position.remaining_quantity = 0.0
    position.close_reason = close_reason
    position.closed_at = now
    position.updated_at = now
    if close_trade_id:
        position.close_trade_id = close_trade_id

    # Update user balance with realized PnL for paper trading
    if not position.live_trading and position.user_id and pnl_usdt != 0.0:
        await update_user_balance(session, position.user_id, pnl_usdt)

    await session.flush()
    return pnl_pct


async def update_user_balance(session: AsyncSession, user_id: str, delta_usdt: float) -> float:
    """Update user balance by adding delta (positive for profit, negative for loss).
    Uses row-level locking to prevent race conditions during concurrent updates."""
    from sqlalchemy import select

    result = await session.execute(
        select(UserModel)
        .where(UserModel.id == user_id)
        .with_for_update()
    )
    user = result.scalar_one_or_none()
    if user:
        current_balance = _safe_float(user.balance_usdt, 0.0)
        new_balance = round(current_balance + delta_usdt, 2)
        user.balance_usdt = max(0.0, new_balance)
        return user.balance_usdt
    return 0.0


async def record_position_close_trade_async(
    session: AsyncSession,
    position: PositionModel,
    exit_price: float,
    close_reason: str,
    order_status: str = "closed",
    order_details: dict | None = None,
) -> TradeModel:
    """Create a synthetic close trade for TP/SL fills detected by the monitor."""
    trade_id = str(uuid.uuid4())
    now = utcnow()
    pnl_pct = await close_position_async(
        session=session,
        position=position,
        exit_price=exit_price,
        close_reason=close_reason,
        close_trade_id=trade_id,
        closed_at=now,
    )
    direction = "close_long" if str(position.direction).lower() == "long" else "close_short"
    close_quantity = position.remaining_quantity if position.remaining_quantity and position.remaining_quantity > 0 else position.quantity
    payload = {
        "position_id": position.id,
        "open_trade_id": position.open_trade_id,
        "ticker": position.ticker,
        "direction": direction,
        "entry_price": position.entry_price,
        "exit_price": exit_price,
        "quantity": close_quantity,
        "original_quantity": position.quantity,
        "remaining_quantity": position.remaining_quantity,
        "pnl_pct": pnl_pct,
        "close_reason": close_reason,
        "order_details": order_details or {},
    }
    trade = TradeModel(
        id=trade_id,
        user_id=position.user_id,
        timestamp=now,
        ticker=position.ticker,
        direction=direction,
        execute=True,
        order_status=order_status,
        pnl_pct=pnl_pct,
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    session.add(trade)
    await session.flush()
    return trade


async def sync_position_from_trade_entry_async(session: AsyncSession, entry: dict) -> dict:
    """
    Maintain a simple open/close position ledger and enrich close trades with realised PnL.
    Async version for use with SQLAlchemy async session.
    """
    if not entry.get("execute"):
        return entry

    status = str(entry.get("order_status") or "").lower()
    if status not in {"filled", "simulated", "closed", "pending"}:
        return entry

    direction = str(entry.get("direction") or "").lower()
    user_id = entry.get("user_id")
    ticker = str(entry.get("ticker") or "").upper().strip()

    if not ticker or direction not in {"long", "short", "close_long", "close_short"}:
        return entry

    order_details = entry.get("order_details") or {}
    analysis = entry.get("analysis") or {}
    exchange_config = entry.get("exchange_config") or {}

    entry_price = _safe_float(order_details.get("entry_price") or entry.get("entry_price"))
    quantity = _safe_float(
        order_details.get("filled_quantity")
        or order_details.get("quantity")
        or entry.get("quantity")
    )
    requested_quantity = _safe_float(order_details.get("requested_quantity") or quantity)
    opened_at = _db_datetime(entry.get("timestamp") or utcnow())

    if quantity > 0 and requested_quantity > 0 and quantity != requested_quantity:
        logger.info(
            f"[Database] Exchange adjusted quantity for {ticker}: "
            f"requested={requested_quantity}, filled={quantity}"
        )

    # Opening a new position
    if direction in {"long", "short"} and entry_price > 0:
        # Check if position already exists for this trade
        existing = await session.execute(
            select(PositionModel).where(PositionModel.open_trade_id == entry.get("id")).limit(1)
        )
        position = existing.scalar_one_or_none()
        if not position:
            take_profit_levels = _take_profit_levels_from_entry(entry)
            tp_order_ids = [
                str(level.get("order_id"))
                for level in take_profit_levels
                if level.get("order_id")
            ]
            stop_loss = _safe_float(
                order_details.get("stop_loss") or analysis.get("suggested_stop_loss"),
                0.0,
            )
            leverage = _safe_float(
                order_details.get("recommended_leverage") or analysis.get("recommended_leverage"),
                1.0,
            )
            live_trading = _safe_bool(exchange_config.get("live_trading"), status != "simulated")
            order_type = str(order_details.get("order_type") or "market").lower()
            signal_timeframe = (entry.get("signal") or {}).get("timeframe")
            timeout_overrides = (exchange_config or {}).get("limit_timeout_overrides") or {}
            limit_timeout_secs = _safe_float(
                order_details.get("limit_timeout_secs"),
                float(resolve_limit_timeout_secs(signal_timeframe, timeout_overrides)),
            )
            position_status = "pending" if status == "pending" else "open"

            trailing_stop_config = order_details.get("trailing_stop_config") or {}
            if not trailing_stop_config:
                ts_mode = order_details.get("trailing_stop-mode") or order_details.get("trailing_stop_mode")
                if ts_mode:
                    trailing_stop_config = {
                        "mode": str(ts_mode),
                        "trail_pct": _safe_float(order_details.get("trailing_pct"), 1.0),
                        "activation_profit_pct": _safe_float(order_details.get("trailing_activation_profit_pct"), 1.0),
                        "trailing_step_pct": _safe_float(order_details.get("trailing_step_pct"), 0.5),
                    }

            position = PositionModel(
                user_id=user_id,
                ticker=ticker,
                direction=direction,
                status=position_status,
                entry_price=entry_price,
                quantity=quantity,
                remaining_quantity=quantity,
                opened_at=opened_at,
                open_trade_id=entry.get("id"),
                entry_order_id=str(order_details.get("order_id") or ""),
                order_type=order_type,
                limit_timeout_secs=limit_timeout_secs,
                stop_loss=stop_loss if stop_loss > 0 else None,
                take_profit_json=json.dumps(take_profit_levels, ensure_ascii=False, default=str),
                stop_loss_order_id=str(order_details.get("stop_loss_order_id") or ""),
                take_profit_order_ids_json=json.dumps(tp_order_ids, ensure_ascii=False),
                trailing_stop_config_json=json.dumps(trailing_stop_config, ensure_ascii=False) if trailing_stop_config else "{}",
                exchange=str(exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name),
                live_trading=live_trading,
                sandbox_mode=_safe_bool(exchange_config.get("sandbox_mode"), False),
                leverage=max(1.0, leverage),
                margin=(entry_price * quantity / leverage) if entry_price > 0 and quantity > 0 else 0,
                liquidation_price=_safe_float(order_details.get("liquidation_price")),
                strategy_name=str(entry.get("strategy_name") or ""),
                user_risk_profile=str(entry.get("user_risk_profile") or "balanced"),
                last_price=entry_price,
                updated_at=opened_at,
            )
            session.add(position)
            await session.flush()
        entry["position_id"] = position.id
        entry["position_event"] = "opened"
        return entry

    # Closing a position
    open_direction = "long" if direction == "close_long" else "short"

    # Find the matching open position
    query = select(PositionModel).where(
        PositionModel.status.in_(["open", "pending"]),
        PositionModel.direction == open_direction,
    )
    if user_id:
        query = query.where(PositionModel.user_id == user_id)
    else:
        query = query.where(PositionModel.user_id.is_(None))
    query = query.order_by(PositionModel.opened_at.desc())

    result = await session.execute(query)
    target_key = position_symbol_key(ticker)
    candidates = [
        row for row in result.scalars().all()
        if position_symbol_key(row.ticker) == target_key
    ]
    position = candidates[0] if candidates else None

    if not position:
        return entry

    exit_price = _safe_float(
        order_details.get("exit_price")
        or order_details.get("average")
        or order_details.get("entry_price")
        or entry.get("entry_price")
    )
    if exit_price <= 0:
        return entry

    entry["position_id"] = position.id
    entry["position_event"] = "closed"
    entry["close_reason"] = "manual_close"
    entry["pnl_pct"] = await close_position_async(
        session=session,
        position=position,
        exit_price=exit_price,
        close_reason="manual_close",
        close_trade_id=entry.get("id"),
    )
    entry["order_status"] = "closed"
    await session.flush()

    return entry


async def get_trade_logs_async(session: AsyncSession, days: int = 30, user_id: str | None = None) -> list[dict]:
    """Get trade logs for the last N days."""
    since = utcnow() - timedelta(days=max(1, min(int(days), 365)))

    query = select(TradeModel).where(TradeModel.timestamp >= since)
    if user_id:
        query = query.where(TradeModel.user_id == user_id)
    query = query.order_by(TradeModel.timestamp.desc())

    result = await session.execute(query)
    trades = result.scalars().all()

    # Parse payload_json for each trade
    logs = []
    for trade in trades:
        try:
            payload = json.loads(trade.payload_json)
            logs.append(payload)
        except (json.JSONDecodeError, TypeError):
            # Fallback to trade model fields
            logs.append({
                "id": trade.id,
                "user_id": trade.user_id,
                "timestamp": trade.timestamp.isoformat() if trade.timestamp else None,
                "ticker": trade.ticker,
                "direction": trade.direction,
                "execute": trade.execute,
                "order_status": trade.order_status,
                "pnl_pct": trade.pnl_pct,
            })

    return logs


# ─────────────────────────────────────────────
# Webhook Event CRUD
# ─────────────────────────────────────────────

async def record_webhook_event(
    session: AsyncSession,
    user_id: str | None,
    fingerprint: str,
    ticker: str,
    direction: str,
    status: str,
    status_code: int,
    reason: str,
    client_ip: str,
    payload: dict,
) -> WebhookEventModel:
    """Record a webhook event."""
    try:
        event = WebhookEventModel(
            user_id=user_id,
            fingerprint=fingerprint,
            ticker=ticker,
            direction=direction,
            status=status,
            status_code=status_code,
            reason=reason,
            client_ip=client_ip,
            payload_json=json.dumps(payload, default=str),
        )
        session.add(event)
        await session.flush()
        return event
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            result = await session.execute(
                select(WebhookEventModel).where(WebhookEventModel.fingerprint == fingerprint).order_by(WebhookEventModel.created_at.desc()).limit(1)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.status = status
                existing.status_code = status_code
                existing.reason = reason
                existing.payload_json = json.dumps(payload, default=str)
                await session.flush()
                return existing
        raise


async def has_recent_webhook_event(session: AsyncSession, fingerprint: str, window_secs: int = 300) -> bool:
    """Check if a webhook with this fingerprint was recently processed."""
    cutoff = utcnow() - timedelta(seconds=window_secs)
    result = await session.execute(
        select(WebhookEventModel)
        .where(WebhookEventModel.fingerprint == fingerprint, WebhookEventModel.created_at >= cutoff)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


# ─────────────────────────────────────────────
# Admin Settings CRUD
# ─────────────────────────────────────────────

async def get_admin_setting(session: AsyncSession, key: str, default: str = "") -> str:
    """Get an admin setting value."""
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == key)
    )
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def set_admin_setting(session: AsyncSession, key: str, value: str):
    """Set an admin setting value."""
    result = await session.execute(
        select(AdminSettingModel).where(AdminSettingModel.key == key)
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
        setting.updated_at = utcnow()
    else:
        setting = AdminSettingModel(key=key, value=value)
        session.add(setting)


# ─────────────────────────────────────────────
# Seed Default Data
# ─────────────────────────────────────────────

_BOOTSTRAP_PASSWORD_FILE = Path("data") / "bootstrap_admin_password.txt"


def _generate_bootstrap_admin_password(length: int = 28) -> str:
    """Generate a strong admin bootstrap password with login-form-safe characters."""
    lowers = string.ascii_lowercase
    uppers = string.ascii_uppercase
    digits = string.digits
    specials = "!@#$%^&*_-+="
    alphabet = lowers + uppers + digits + specials

    required = [
        secrets.choice(lowers),
        secrets.choice(uppers),
        secrets.choice(digits),
        secrets.choice(specials),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(max(length, 12) - len(required))]
    chars = required + remaining
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _read_bootstrap_admin_password(path: Path = _BOOTSTRAP_PASSWORD_FILE) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "password" and value.strip():
                return value.strip()
    except OSError as exc:
        logger.warning(f"[Database] Failed to read bootstrap admin password file: {exc}")
    return ""


def _load_or_create_bootstrap_admin_password(
    username: str,
    path: Path = _BOOTSTRAP_PASSWORD_FILE,
) -> tuple[str, Path]:
    existing = _read_bootstrap_admin_password(path)
    if existing:
        return existing, path

    password = _generate_bootstrap_admin_password()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# QuantPilot AI bootstrap admin password\n"
        "# Generated because DEFAULT_ADMIN_PASSWORD was left blank.\n"
        "# Delete this file after changing the admin password.\n"
        f"username={username}\n"
        f"password={password}\n"
    )
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return password, path


async def seed_defaults(session: AsyncSession):
    """Seed default admin user and subscription plans."""
    from core.security import hash_password

    # Check for existing admin
    result = await session.execute(
        select(UserModel).where(UserModel.role == "admin").limit(1)
    )
    admin = result.scalar_one_or_none()

    if not admin:
        admin_username = settings.default_admin_username.lower().strip()
        admin_password = settings.default_admin_password
        bootstrap_path = None
        if not admin_password:
            admin_password, bootstrap_path = _load_or_create_bootstrap_admin_password(admin_username)

        admin = UserModel(
            username=admin_username,
            email=settings.default_admin_email.lower().strip(),
            password_hash=hash_password(admin_password),
            role="admin",
        )
        session.add(admin)
        if bootstrap_path:
            logger.warning(
                "[Database] Bootstrap admin created: {}. Password file: {}. Change it after first login.",
                admin.username,
                bootstrap_path,
            )
        else:
            logger.warning(f"[Database] Default admin created from DEFAULT_ADMIN_PASSWORD: {admin.username}")

    # Check for existing plans
    result = await session.execute(select(SubscriptionPlanModel).limit(1))
    if result.scalar_one_or_none() is None:
        plans = [
            SubscriptionPlanModel(
                name="Free Trial",
                description="7-day free trial with limited signals",
                price_usdt=0.0,
                duration_days=7,
                features_json=json.dumps(["5 signals/day", "Basic AI analysis"]),
                max_signals_per_day=5,
            ),
            SubscriptionPlanModel(
                name="Basic Monthly",
                description="Standard monthly plan",
                price_usdt=29.99,
                duration_days=30,
                features_json=json.dumps(["Unlimited signals", "Full AI analysis", "Email support"]),
                max_signals_per_day=0,
            ),
            SubscriptionPlanModel(
                name="Pro Monthly",
                description="Professional monthly plan",
                price_usdt=79.99,
                duration_days=30,
                features_json=json.dumps(["Unlimited signals", "Full AI analysis", "Multi-TP & Trailing Stop", "Priority support"]),
                max_signals_per_day=0,
            ),
            SubscriptionPlanModel(
                name="Pro Yearly",
                description="Professional yearly plan (save 30%)",
                price_usdt=599.99,
                duration_days=365,
                features_json=json.dumps(["Everything in Pro Monthly", "30% discount", "Dedicated support"]),
                max_signals_per_day=0,
            ),
        ]
        for plan in plans:
            session.add(plan)
        logger.info("[Database] Default subscription plans created")

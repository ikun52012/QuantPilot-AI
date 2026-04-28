"""Alembic migration script template."""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone


def _utcnow():
    """Get current UTC time as naive datetime for PostgreSQL compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(32), unique=True, nullable=False),
        sa.Column("email", sa.String(128), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(20), default="user"),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("webhook_secret_hash", sa.String(128), nullable=True),
        sa.Column("exchange_config_encrypted", sa.Text, nullable=True),
        sa.Column("ai_config_encrypted", sa.Text, nullable=True),
        sa.Column("tp_config_json", sa.Text, default="[]"),
        sa.Column("risk_config_json", sa.Text, default="{}"),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
        sa.Column("updated_at", sa.DateTime, default=_utcnow, onupdate=_utcnow),
    )

    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("price_usdt", sa.Float, nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("max_signals_per_day", sa.Integer, default=0),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("subscription_plans.id"), nullable=False),
        sa.Column("status", sa.String(20), default="pending"),
        sa.Column("start_date", sa.DateTime, nullable=True),
        sa.Column("end_date", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id"), nullable=True),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("currency", sa.String(12), default="USDT"),
        sa.Column("network", sa.String(20), default="TRC20"),
        sa.Column("tx_hash", sa.String(200), default=""),
        sa.Column("wallet_address", sa.String(128), default=""),
        sa.Column("status", sa.String(20), default="pending"),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
        sa.Column("confirmed_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("ticker", sa.String(40), default=""),
        sa.Column("direction", sa.String(20), default=""),
        sa.Column("execute", sa.Boolean, default=False),
        sa.Column("order_status", sa.String(20), default=""),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("quantity", sa.Float, default=0.0),
        sa.Column("pnl_pct", sa.Float, default=0.0),
        sa.Column("pnl_usdt", sa.Float, default=0.0),
        sa.Column("fees_usdt", sa.Float, default=0.0),
        sa.Column("fees_pct", sa.Float, default=0.0),
        sa.Column("execution_latency_ms", sa.Integer, default=0),
        sa.Column("strategy_name", sa.String(120), default=""),
        sa.Column("signal_source", sa.String(20), default="tradingview"),
        sa.Column("payload_json", sa.Text, nullable=False),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("ticker", sa.String(40), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), default="open"),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, default=0.0),
        sa.Column("remaining_quantity", sa.Float, default=0.0),
        sa.Column("opened_at", sa.DateTime, nullable=False),
        sa.Column("open_trade_id", sa.String(36), nullable=True),
        sa.Column("entry_order_id", sa.String(128), default=""),
        sa.Column("stop_loss", sa.Float, nullable=True),
        sa.Column("take_profit_json", sa.Text, default="[]"),
        sa.Column("stop_loss_order_id", sa.String(128), default=""),
        sa.Column("take_profit_order_ids_json", sa.Text, default="[]"),
        sa.Column("trailing_stop_config_json", sa.Text, default="{}"),
        sa.Column("exchange", sa.String(40), default=""),
        sa.Column("live_trading", sa.Boolean, default=False),
        sa.Column("sandbox_mode", sa.Boolean, default=False),
        sa.Column("leverage", sa.Float, default=1.0),
        sa.Column("strategy_name", sa.String(120), default=""),
        sa.Column("user_risk_profile", sa.String(20), default="balanced"),
        sa.Column("realized_pnl_pct", sa.Float, default=0.0),
        sa.Column("current_pnl_pct", sa.Float, default=0.0),
        sa.Column("unrealized_pnl_usdt", sa.Float, default=0.0),
        sa.Column("fees_total_usdt", sa.Float, default=0.0),
        sa.Column("last_price", sa.Float, nullable=True),
        sa.Column("close_reason", sa.String(80), default=""),
        sa.Column("updated_at", sa.DateTime, default=_utcnow),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("pnl_pct", sa.Float, default=0.0),
        sa.Column("closed_at", sa.DateTime, nullable=True),
        sa.Column("close_trade_id", sa.String(36), nullable=True),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("ticker", sa.String(40), default=""),
        sa.Column("direction", sa.String(20), default=""),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_code", sa.Integer, default=200),
        sa.Column("reason", sa.Text, default=""),
        sa.Column("client_ip", sa.String(45), default=""),
        sa.Column("payload_json", sa.Text, default="{}"),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "admin_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.String(36), nullable=True),
        sa.Column("admin_username", sa.String(32), default=""),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(50), default=""),
        sa.Column("target_id", sa.String(36), default=""),
        sa.Column("summary", sa.Text, default=""),
        sa.Column("client_ip", sa.String(45), default=""),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "invite_codes",
        sa.Column("code", sa.String(80), primary_key=True),
        sa.Column("note", sa.Text, default=""),
        sa.Column("max_uses", sa.Integer, default=1),
        sa.Column("used_count", sa.Integer, default=0),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_table(
        "filter_block_stats",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("filter_name", sa.String(40), nullable=False),
        sa.Column("ticker", sa.String(40), default=""),
        sa.Column("block_count", sa.Integer, default=0),
        sa.Column("last_block_at", sa.DateTime, nullable=True),
        sa.Column("total_weight_reduced", sa.Float, default=0.0),
        sa.Column("created_at", sa.DateTime, default=_utcnow),
    )

    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_trades_user_id", "trades", ["user_id"])
    op.create_index("ix_trades_timestamp", "trades", ["timestamp"])
    op.create_index("ix_positions_user_id", "positions", ["user_id"])
    op.create_index("ix_positions_ticker", "positions", ["ticker"])
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_index("ix_webhook_events_fingerprint", "webhook_events", ["fingerprint"])
    op.create_index("ix_webhook_events_created_at", "webhook_events", ["created_at"])


def downgrade():
    op.drop_index("ix_webhook_events_created_at", "webhook_events")
    op.drop_index("ix_webhook_events_fingerprint", "webhook_events")
    op.drop_index("ix_positions_status", "positions")
    op.drop_index("ix_positions_ticker", "positions")
    op.drop_index("ix_positions_user_id", "positions")
    op.drop_index("ix_trades_timestamp", "trades")
    op.drop_index("ix_trades_user_id", "trades")
    op.drop_index("ix_users_email", "users")
    op.drop_index("ix_users_username", "users")

    op.drop_table("filter_block_stats")
    op.drop_table("invite_codes")
    op.drop_table("admin_audit_logs")
    op.drop_table("admin_settings")
    op.drop_table("webhook_events")
    op.drop_table("positions")
    op.drop_table("trades")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("subscription_plans")
    op.drop_table("users")
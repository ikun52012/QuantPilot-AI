"""Align Alembic schema with current runtime models.

Revision ID: 002_align_runtime_schema
Revises: 001_initial_schema
Create Date: 2026-04-27
"""
from alembic import op
import sqlalchemy as sa


revision = "002_align_runtime_schema"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def _tables(inspector) -> set[str]:
    return set(inspector.get_table_names())


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _add_missing_columns(inspector, table_name: str, columns: list[sa.Column]) -> None:
    if table_name not in _tables(inspector):
        return
    existing = _columns(inspector, table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _create_index_if_missing(inspector, name: str, table_name: str, columns: list[str]) -> None:
    if table_name in _tables(inspector) and name not in _indexes(inspector, table_name):
        op.create_index(name, table_name, columns)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = _tables(inspector)

    _add_missing_columns(inspector, "users", [
        sa.Column("balance_usdt", sa.Float(), server_default="0"),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("settings_json", sa.Text(), server_default="{}"),
        sa.Column("webhook_secret", sa.String(128), server_default=""),
        sa.Column("live_trading_allowed", sa.Boolean(), server_default=sa.false()),
        sa.Column("max_leverage", sa.Integer(), server_default="20"),
        sa.Column("max_position_pct", sa.Float(), server_default="10"),
        sa.Column("token_version", sa.Integer(), server_default="0"),
        sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("totp_secret", sa.String(256), server_default=""),
        sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false()),
        sa.Column("totp_recovery_codes_json", sa.Text(), server_default="[]"),
    ])
    _add_missing_columns(inspector, "subscription_plans", [
        sa.Column("features_json", sa.Text(), server_default="[]"),
    ])
    _add_missing_columns(inspector, "invite_codes", [
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("last_used_by", sa.String(36), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    ])
    _add_missing_columns(inspector, "positions", [
        sa.Column("order_type", sa.String(40), server_default="market"),
        sa.Column("limit_timeout_secs", sa.Float(), server_default="300"),
    ])

    if "redeem_codes" not in tables:
        op.create_table(
            "redeem_codes",
            sa.Column("code", sa.String(80), primary_key=True),
            sa.Column("plan_id", sa.String(36), sa.ForeignKey("subscription_plans.id"), nullable=True),
            sa.Column("duration_days", sa.Integer(), server_default="0"),
            sa.Column("balance_usdt", sa.Float(), server_default="0"),
            sa.Column("note", sa.Text(), server_default=""),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
            sa.Column("redeemed_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.String(36), nullable=True),
        )

    if "order_events" not in tables:
        op.create_table(
            "order_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("position_id", sa.String(36), nullable=True),
            sa.Column("trade_id", sa.String(36), nullable=True),
            sa.Column("client_order_id", sa.String(128), server_default=""),
            sa.Column("exchange_order_id", sa.String(128), server_default=""),
            sa.Column("ticker", sa.String(40), server_default=""),
            sa.Column("direction", sa.String(20), server_default=""),
            sa.Column("order_type", sa.String(40), server_default=""),
            sa.Column("status", sa.String(30), server_default="created"),
            sa.Column("retry_state", sa.String(30), server_default="not_required"),
            sa.Column("attempt_count", sa.Integer(), server_default="0"),
            sa.Column("last_error", sa.Text(), server_default=""),
            sa.Column("payload_json", sa.Text(), server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        )

    if "strategy_states" not in tables:
        op.create_table(
            "strategy_states",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("strategy_type", sa.String(32), nullable=False),
            sa.Column("ticker", sa.String(40), server_default=""),
            sa.Column("name", sa.String(120), server_default=""),
            sa.Column("status", sa.String(30), server_default="active"),
            sa.Column("config_json", sa.Text(), server_default="{}"),
            sa.Column("state_json", sa.Text(), server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if "shared_signals" not in tables:
        op.create_table(
            "shared_signals",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("username", sa.String(64), server_default=""),
            sa.Column("ticker", sa.String(40), server_default=""),
            sa.Column("direction", sa.String(20), server_default=""),
            sa.Column("entry_price", sa.Float(), server_default="0"),
            sa.Column("stop_loss", sa.Float(), nullable=True),
            sa.Column("take_profit", sa.Float(), nullable=True),
            sa.Column("confidence", sa.Float(), server_default="0"),
            sa.Column("strategy_name", sa.String(120), server_default=""),
            sa.Column("reason", sa.Text(), server_default=""),
            sa.Column("status", sa.String(20), server_default="active"),
            sa.Column("is_private", sa.Boolean(), server_default=sa.false()),
            sa.Column("subscribers_count", sa.Integer(), server_default="0"),
            sa.Column("executions_count", sa.Integer(), server_default="0"),
            sa.Column("success_rate", sa.Float(), server_default="0"),
            sa.Column("stats_json", sa.Text(), server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )

    if "signal_subscriptions" not in tables:
        op.create_table(
            "signal_subscriptions",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("signal_id", sa.String(80), sa.ForeignKey("shared_signals.id"), nullable=False),
            sa.Column("auto_execute", sa.Boolean(), server_default=sa.false()),
            sa.Column("max_position_pct", sa.Float(), server_default="10"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    _create_index_if_missing(inspector, "idx_users_webhook_secret_hash", "users", ["webhook_secret_hash"])
    _create_index_if_missing(inspector, "idx_trades_user_timestamp", "trades", ["user_id", "timestamp"])
    _create_index_if_missing(inspector, "idx_webhook_fingerprint_created", "webhook_events", ["fingerprint", "created_at"])
    _create_index_if_missing(inspector, "idx_positions_user_status", "positions", ["user_id", "status"])
    _create_index_if_missing(inspector, "idx_order_events_status_retry", "order_events", ["status", "retry_state", "next_retry_at"])
    _create_index_if_missing(inspector, "idx_order_events_user_created", "order_events", ["user_id", "created_at"])
    _create_index_if_missing(inspector, "idx_strategy_states_user_type", "strategy_states", ["user_id", "strategy_type"])
    _create_index_if_missing(inspector, "idx_strategy_states_type_status", "strategy_states", ["strategy_type", "status"])
    _create_index_if_missing(inspector, "idx_shared_signals_status_created", "shared_signals", ["status", "created_at"])
    _create_index_if_missing(inspector, "idx_shared_signals_ticker_direction", "shared_signals", ["ticker", "direction"])
    _create_index_if_missing(inspector, "idx_signal_subscriptions_user", "signal_subscriptions", ["user_id"])
    _create_index_if_missing(inspector, "idx_signal_subscriptions_signal", "signal_subscriptions", ["signal_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table_name in [
        ("idx_signal_subscriptions_signal", "signal_subscriptions"),
        ("idx_signal_subscriptions_user", "signal_subscriptions"),
        ("idx_shared_signals_ticker_direction", "shared_signals"),
        ("idx_shared_signals_status_created", "shared_signals"),
        ("idx_strategy_states_type_status", "strategy_states"),
        ("idx_strategy_states_user_type", "strategy_states"),
        ("idx_order_events_user_created", "order_events"),
        ("idx_order_events_status_retry", "order_events"),
    ]:
        if table_name in _tables(inspector) and name in _indexes(inspector, table_name):
            op.drop_index(name, table_name=table_name)

    for table_name in ["signal_subscriptions", "shared_signals", "strategy_states", "order_events", "redeem_codes"]:
        if table_name in _tables(sa.inspect(bind)):
            op.drop_table(table_name)

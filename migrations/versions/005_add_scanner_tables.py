"""Add automatic market scanner state and audit tables.

Revision ID: 005_add_scanner_tables
Revises: 004_widen_ticker
Create Date: 2026-05-24
"""
import sqlalchemy as sa
from alembic import op

revision = "005_add_scanner_tables"
down_revision = "004_widen_ticker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scanner_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(40), nullable=False, server_default="admin"),
        sa.Column("date_key", sa.String(10), nullable=False),
        sa.Column("scan_count", sa.Integer, server_default="0"),
        sa.Column("ai_call_count", sa.Integer, server_default="0"),
        sa.Column("signal_count", sa.Integer, server_default="0"),
        sa.Column("data_failure_streak", sa.Integer, server_default="0"),
        sa.Column("last_scan_at", sa.DateTime, nullable=True),
        sa.Column("last_data_failure_at", sa.DateTime, nullable=True),
        sa.Column("degraded_mode", sa.String(40), server_default=""),
        sa.Column("degraded_reason", sa.Text, server_default=""),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("scope", "date_key", name="uq_scanner_states_scope_date"),
    )
    op.create_index("idx_scanner_states_scope_date", "scanner_states", ["scope", "date_key"])

    op.create_table(
        "scanner_setup_locks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(40), nullable=False, server_default="admin"),
        sa.Column("setup_hash", sa.String(64), nullable=False),
        sa.Column("watch_symbol", sa.String(60), server_default=""),
        sa.Column("exchange_symbol", sa.String(60), server_default=""),
        sa.Column("direction", sa.String(20), server_default=""),
        sa.Column("timeframe", sa.String(20), server_default=""),
        sa.Column("setup_type", sa.String(80), server_default=""),
        sa.Column("price_zone", sa.String(80), server_default=""),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("scope", "setup_hash", name="uq_scanner_setup_locks_scope_hash"),
    )
    op.create_index("idx_scanner_setup_hash_expires", "scanner_setup_locks", ["setup_hash", "expires_at"])
    op.create_index("idx_scanner_symbol_expires", "scanner_setup_locks", ["scope", "exchange_symbol", "expires_at"])

    op.create_table(
        "scanner_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(40), nullable=False, server_default="admin"),
        sa.Column("run_id", sa.String(64), server_default=""),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("watch_symbol", sa.String(60), server_default=""),
        sa.Column("exchange_symbol", sa.String(60), server_default=""),
        sa.Column("direction", sa.String(20), server_default=""),
        sa.Column("score", sa.Float, server_default="0"),
        sa.Column("setup_hash", sa.String(64), server_default=""),
        sa.Column("reason", sa.Text, server_default=""),
        sa.Column("payload_json", sa.Text, server_default="{}"),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("idx_scanner_audit_run", "scanner_audits", ["run_id"])
    op.create_index("idx_scanner_audit_created", "scanner_audits", ["created_at"])
    op.create_index("idx_scanner_audit_symbol_created", "scanner_audits", ["exchange_symbol", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_scanner_audit_symbol_created", table_name="scanner_audits")
    op.drop_index("idx_scanner_audit_created", table_name="scanner_audits")
    op.drop_index("idx_scanner_audit_run", table_name="scanner_audits")
    op.drop_table("scanner_audits")

    op.drop_index("idx_scanner_symbol_expires", table_name="scanner_setup_locks")
    op.drop_index("idx_scanner_setup_hash_expires", table_name="scanner_setup_locks")
    op.drop_table("scanner_setup_locks")

    op.drop_index("idx_scanner_states_scope_date", table_name="scanner_states")
    op.drop_table("scanner_states")

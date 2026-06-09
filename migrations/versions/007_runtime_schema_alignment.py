"""Align Alembic schema with runtime audit/scanner models.

Revision ID: 007_runtime_schema_alignment
Revises: 006_webhook_active_fingerprint
Create Date: 2026-06-07
"""

import sqlalchemy as sa
from alembic import op

revision = "007_runtime_schema_alignment"
down_revision = "006_webhook_active_fingerprint"
branch_labels = None
depends_on = None


def _columns(inspector, table_name: str) -> set[str]:
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(inspector, table_name: str, name: str) -> bool:
    if table_name not in inspector.get_table_names():
        return False
    return any(index["name"] == name for index in inspector.get_indexes(table_name))


def _add_column_if_missing(inspector, table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(inspector, table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(inspector, name: str, table_name: str, columns: list[str]) -> None:
    if not _index_exists(inspector, table_name, name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "signal_decision_audits" not in tables:
        op.create_table(
            "signal_decision_audits",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("fingerprint", sa.String(64), server_default=""),
            sa.Column("ticker", sa.String(60), server_default=""),
            sa.Column("direction", sa.String(20), server_default=""),
            sa.Column("stage", sa.String(40), nullable=False),
            sa.Column("outcome", sa.String(40), server_default=""),
            sa.Column("reason", sa.Text, server_default=""),
            sa.Column("payload_json", sa.Text, server_default="{}"),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )
        inspector = sa.inspect(bind)

    if "signal_decision_audits" in inspector.get_table_names():
        _create_index_if_missing(inspector, "idx_signal_decision_audits_fingerprint_stage", "signal_decision_audits", ["fingerprint", "stage"])
        _create_index_if_missing(inspector, "idx_signal_decision_audits_user_created", "signal_decision_audits", ["user_id", "created_at"])
        _create_index_if_missing(inspector, "ix_signal_decision_audits_user_id", "signal_decision_audits", ["user_id"])
        _create_index_if_missing(inspector, "ix_signal_decision_audits_fingerprint", "signal_decision_audits", ["fingerprint"])
        _create_index_if_missing(inspector, "ix_signal_decision_audits_ticker", "signal_decision_audits", ["ticker"])
        _create_index_if_missing(inspector, "ix_signal_decision_audits_stage", "signal_decision_audits", ["stage"])
        _create_index_if_missing(inspector, "ix_signal_decision_audits_created_at", "signal_decision_audits", ["created_at"])

    if "scanner_states" in inspector.get_table_names():
        for column in (
            sa.Column("signal_wins", sa.Integer, server_default="0"),
            sa.Column("signal_losses", sa.Integer, server_default="0"),
            sa.Column("signal_win_rate", sa.Float, server_default="0"),
            sa.Column("adaptive_min_score", sa.Float, server_default="0"),
            sa.Column("win_rate_history_json", sa.Text, server_default="[]"),
            sa.Column("cooldown_level", sa.Integer, server_default="0"),
            sa.Column("last_win_rate_update_at", sa.DateTime, nullable=True),
        ):
            _add_column_if_missing(inspector, "scanner_states", column)
            inspector = sa.inspect(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "signal_decision_audits" in inspector.get_table_names():
        for name in (
            "ix_signal_decision_audits_created_at",
            "ix_signal_decision_audits_stage",
            "ix_signal_decision_audits_ticker",
            "ix_signal_decision_audits_fingerprint",
            "ix_signal_decision_audits_user_id",
            "idx_signal_decision_audits_user_created",
            "idx_signal_decision_audits_fingerprint_stage",
        ):
            if _index_exists(inspector, "signal_decision_audits", name):
                op.drop_index(name, table_name="signal_decision_audits")
        op.drop_table("signal_decision_audits")

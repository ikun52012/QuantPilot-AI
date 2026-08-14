"""Manage the full AI decision audit table and retention timestamp.

Revision ID: 009_ai_decision_audit
Revises: 008_widen_runtime_columns
Create Date: 2026-07-23
"""

import sqlalchemy as sa
from alembic import op

revision = "009_ai_decision_audit"
down_revision = "008_widen_runtime_columns"
branch_labels = None
depends_on = None


def _index_exists(inspector, table_name: str, name: str) -> bool:
    return any(index["name"] == name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "ai_decision_log" not in tables:
        op.create_table(
            "ai_decision_log",
            sa.Column("decision_id", sa.String(36), primary_key=True),
            sa.Column("timestamp", sa.String(40), nullable=False),
            sa.Column("ticker", sa.String(60), nullable=True),
            sa.Column("direction", sa.String(20), nullable=True),
            sa.Column("signal_price", sa.Float, nullable=True),
            sa.Column("timeframe", sa.String(20), nullable=True),
            sa.Column("strategy", sa.String(120), nullable=True),
            sa.Column("user_id", sa.String(36), nullable=True),
            sa.Column("provider", sa.String(40), nullable=True),
            sa.Column("model_id", sa.String(120), nullable=True),
            sa.Column("system_prompt", sa.Text, nullable=True),
            sa.Column("user_prompt", sa.Text, nullable=True),
            sa.Column("raw_response", sa.Text, nullable=True),
            sa.Column("analysis_json", sa.Text, nullable=True),
            sa.Column("market_context_json", sa.Text, nullable=True),
            sa.Column("enhanced_data_json", sa.Text, nullable=True),
            sa.Column("recommendation", sa.String(40), nullable=True),
            sa.Column("confidence", sa.Float, nullable=True),
            sa.Column("risk_score", sa.Float, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )
        inspector = sa.inspect(bind)
    else:
        inspected_columns = {
            column["name"]: column for column in inspector.get_columns("ai_decision_log")
        }
        columns = set(inspected_columns)
        if "created_at" not in columns:
            op.add_column("ai_decision_log", sa.Column("created_at", sa.DateTime, nullable=True))
            inspector = sa.inspect(bind)
        if bind.dialect.name == "postgresql":
            for column_name in (
                "analysis_json",
                "market_context_json",
                "enhanced_data_json",
            ):
                column = inspected_columns.get(column_name)
                if column is not None and not isinstance(column["type"], sa.Text):
                    op.alter_column(
                        "ai_decision_log",
                        column_name,
                        type_=sa.Text(),
                        existing_type=column["type"],
                        postgresql_using=f"{column_name}::text",
                    )

        if bind.dialect.name == "postgresql":
            op.execute(sa.text(
                """
                UPDATE ai_decision_log
                SET created_at = CAST(timestamp AS TIMESTAMPTZ) AT TIME ZONE 'UTC'
                WHERE created_at IS NULL
                  AND timestamp ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                """
            ))
        elif bind.dialect.name == "sqlite":
            op.execute(sa.text(
                """
                UPDATE ai_decision_log
                SET created_at = datetime(timestamp)
                WHERE created_at IS NULL
                """
            ))

    if not _index_exists(inspector, "ai_decision_log", "idx_ai_decision_log_ticker_ts"):
        op.create_index(
            "idx_ai_decision_log_ticker_ts",
            "ai_decision_log",
            ["ticker", "timestamp"],
        )
    inspector = sa.inspect(bind)
    if not _index_exists(inspector, "ai_decision_log", "idx_ai_decision_log_created_at"):
        op.create_index(
            "idx_ai_decision_log_created_at",
            "ai_decision_log",
            ["created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_decision_log" in inspector.get_table_names():
        op.drop_table("ai_decision_log")

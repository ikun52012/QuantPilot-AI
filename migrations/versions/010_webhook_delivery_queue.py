"""Add durable webhook delivery queue metadata.

Revision ID: 010_webhook_delivery_queue
Revises: 009_ai_decision_audit
"""

import sqlalchemy as sa
from alembic import op

revision = "010_webhook_delivery_queue"
down_revision = "009_ai_decision_audit"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("webhook_events")}


def upgrade() -> None:
    columns = _column_names()
    if "updated_at" not in columns:
        op.add_column("webhook_events", sa.Column("updated_at", sa.DateTime(), nullable=True))
    if "attempt_count" not in columns:
        op.add_column(
            "webhook_events",
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "next_attempt_at" not in columns:
        op.add_column("webhook_events", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))

    # Existing rows predate the lease timestamp. Preserve their original
    # receive time so stale "processing" events are recoverable immediately.
    op.execute(
        sa.text(
            "UPDATE webhook_events "
            "SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
        )
    )

    inspector = sa.inspect(op.get_bind())
    index_names = {index["name"] for index in inspector.get_indexes("webhook_events")}
    if "idx_webhook_status_retry" not in index_names:
        op.create_index(
            "idx_webhook_status_retry",
            "webhook_events",
            ["status", "next_attempt_at", "updated_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    index_names = {index["name"] for index in inspector.get_indexes("webhook_events")}
    if "idx_webhook_status_retry" in index_names:
        op.drop_index("idx_webhook_status_retry", table_name="webhook_events")
    columns = _column_names()
    if "next_attempt_at" in columns:
        op.drop_column("webhook_events", "next_attempt_at")
    if "attempt_count" in columns:
        op.drop_column("webhook_events", "attempt_count")
    if "updated_at" in columns:
        op.drop_column("webhook_events", "updated_at")

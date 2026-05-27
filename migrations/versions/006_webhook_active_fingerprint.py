"""Add active webhook fingerprint uniqueness.

Revision ID: 006_webhook_active_fingerprint
Revises: 005_add_scanner_tables
Create Date: 2026-05-27
"""
import sqlalchemy as sa
from alembic import op

revision = "006_webhook_active_fingerprint"
down_revision = "005_add_scanner_tables"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_webhook_active_fingerprint"
WHERE_CLAUSE = "status IN ('received','reserved','retrying','processing')"


def _index_exists(inspector, table_name: str, name: str) -> bool:
    return any(index["name"] == name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"sqlite", "postgresql"}:
        return
    inspector = sa.inspect(bind)
    if "webhook_events" not in inspector.get_table_names():
        return
    if _index_exists(inspector, "webhook_events", INDEX_NAME):
        return
    op.create_index(
        INDEX_NAME,
        "webhook_events",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text(WHERE_CLAUSE),
        sqlite_where=sa.text(WHERE_CLAUSE),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"sqlite", "postgresql"}:
        return
    inspector = sa.inspect(bind)
    if "webhook_events" in inspector.get_table_names() and _index_exists(inspector, "webhook_events", INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name="webhook_events")

"""Widen remaining runtime columns to match ORM metadata.

Revision ID: 008_widen_runtime_columns
Revises: 007_runtime_schema_alignment
Create Date: 2026-06-07
"""

import sqlalchemy as sa
from alembic import op

revision = "008_widen_runtime_columns"
down_revision = "007_runtime_schema_alignment"
branch_labels = None
depends_on = None

STRING_COLUMNS = [
    ("users", "email", 254, 128),
    ("subscription_plans", "name", 100, 64),
    ("order_events", "ticker", 60, 40),
    ("strategy_states", "ticker", 60, 40),
    ("shared_signals", "ticker", 60, 40),
]


def _columns_by_table() -> dict[str, set[str]]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
    }


def _alter_string(table: str, column: str, length: int, existing_length: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.alter_column(
        table,
        column,
        type_=sa.String(length),
        existing_type=sa.String(existing_length),
        existing_nullable=True,
    )


def upgrade() -> None:
    columns = _columns_by_table()
    for table, column, length, existing_length in STRING_COLUMNS:
        if column in columns.get(table, set()):
            _alter_string(table, column, length, existing_length)


def downgrade() -> None:
    columns = _columns_by_table()
    for table, column, length, existing_length in STRING_COLUMNS:
        if column in columns.get(table, set()):
            _alter_string(table, column, existing_length, length)

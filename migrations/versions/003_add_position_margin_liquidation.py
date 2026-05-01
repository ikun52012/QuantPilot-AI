"""Add margin and liquidation_price fields to positions table.

Revision ID: 003
Revises: 002_align_runtime_schema
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = "002_align_runtime_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("margin", sa.Float, default=0.0))
    op.add_column("positions", sa.Column("liquidation_price", sa.Float, nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "liquidation_price")
    op.drop_column("positions", "margin")
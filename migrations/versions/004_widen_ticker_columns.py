"""
Widen ticker columns from 40 to 60 chars to accommodate exchange suffixes.

Exchange symbol resolution may append suffixes like :USDT to tickers,
which can exceed the 40-character limit.
"""
import sqlalchemy as sa
from alembic import op

revision = "004_widen_ticker"
down_revision = "003"
branch_labels = None
depends_on = None

COLUMN_TO_60 = [
    ("positions", "ticker"),
    ("trade_log", "ticker"),
    ("replay_log", "ticker"),
    ("filter_stats", "ticker"),
    ("rejected_signals", "ticker"),
    ("webhook_events", "ticker"),
    ("trades", "ticker"),
]

COLUMN_TO_60_OPTIONAL = [
    ("positions", "order_type"),
    ("trade_log", "order_type"),
]


def _existing_columns() -> dict[str, set[str]]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in tables
    }


def _alter_string_column(table: str, column: str, length: int, existing_length: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite does not enforce VARCHAR lengths and cannot ALTER column types directly.
        return
    op.alter_column(
        table,
        column,
        type_=sa.String(length),
        existing_type=sa.String(existing_length),
        existing_nullable=True,
    )


def upgrade() -> None:
    columns_by_table = _existing_columns()
    for table, column in [*COLUMN_TO_60, *COLUMN_TO_60_OPTIONAL]:
        if column in columns_by_table.get(table, set()):
            _alter_string_column(table, column, 60, 40)


def downgrade() -> None:
    columns_by_table = _existing_columns()
    for table, column in [*COLUMN_TO_60, *COLUMN_TO_60_OPTIONAL]:
        if column in columns_by_table.get(table, set()):
            _alter_string_column(table, column, 40, 60)

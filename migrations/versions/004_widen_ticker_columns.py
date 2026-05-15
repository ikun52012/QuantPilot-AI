"""
Widen ticker columns from 40 to 60 chars to accommodate exchange suffixes.

Exchange symbol resolution may append suffixes like :USDT to tickers,
which can exceed the 40-character limit.
"""
from alembic import op

revision = "004_widen_ticker"
down_revision = "003_add_position_margin_liquidation"
branch_labels = None
depends_on = None

TABLES_WITH_TICKER = [
    "trades",
    "webhook_events",
    "positions",
    "trade_log",
    "replay_log",
    "filter_stats",
    "rejected_signals",
]

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


def upgrade() -> None:
    for table, column in COLUMN_TO_60:
        op.alter_column(table, column, type_=op.f("VARCHAR(60)"), existing_type=op.f("VARCHAR(40)"))
    for table, column in COLUMN_TO_60_OPTIONAL:
        try:
            op.alter_column(table, column, type_=op.f("VARCHAR(60)"), existing_type=op.f("VARCHAR(40)"))
        except Exception:
            pass


def downgrade() -> None:
    for table, column in COLUMN_TO_60:
        op.alter_column(table, column, type_=op.f("VARCHAR(40)"), existing_type=op.f("VARCHAR(60)"))
    for table, column in COLUMN_TO_60_OPTIONAL:
        try:
            op.alter_column(table, column, type_=op.f("VARCHAR(40)"), existing_type=op.f("VARCHAR(60)"))
        except Exception:
            pass
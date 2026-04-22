import shutil
import unittest
import os
from pathlib import Path

try:
    import loguru
    import cryptography
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"runtime dependency not installed: {exc.name}")

os.environ.setdefault("APP_ENCRYPTION_KEY", "test-only-fernet-key-do-not-use")

import database  # deprecated: uses legacy synchronous module for test compatibility


class PositionPnlTests(unittest.TestCase):
    """
    Position PnL calculation tests.
    
    Note: This test uses the legacy database.py module for synchronous testing.
    For production code, prefer core/database.py with async SQLAlchemy.
    """
    def setUp(self):
        self.tmp_path = Path.cwd() / ".test_tmp" / self._testMethodName
        if self.tmp_path.exists():
            shutil.rmtree(self.tmp_path, ignore_errors=True)
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.tmp_path / "server.db"
        database.init_database()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        shutil.rmtree(self.tmp_path, ignore_errors=True)

    def test_close_long_updates_realized_pnl(self):
        database.insert_trade_log({
            "id": "open-1",
            "timestamp": "2026-04-21T00:00:00+00:00",
            "user_id": "user-1",
            "ticker": "ETHUSDT",
            "direction": "long",
            "execute": True,
            "entry_price": 100.0,
            "quantity": 1.0,
            "order_status": "simulated",
            "order_details": {"entry_price": 100.0, "quantity": 1.0},
        })
        close_entry = database.sync_position_from_trade_entry({
            "id": "close-1",
            "timestamp": "2026-04-21T00:05:00+00:00",
            "user_id": "user-1",
            "ticker": "ETHUSDT",
            "direction": "close_long",
            "execute": True,
            "entry_price": 110.0,
            "quantity": 1.0,
            "order_status": "simulated",
            "order_details": {"entry_price": 110.0, "quantity": 1.0},
        })
        self.assertAlmostEqual(close_entry["pnl_pct"], 10.0)


if __name__ == "__main__":
    unittest.main()

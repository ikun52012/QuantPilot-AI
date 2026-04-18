"""
TradingView Signal Server - Database Layer
SQLite database for users, subscriptions, and payments.
"""
import sqlite3
import json
import uuid
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

DB_PATH = Path(__file__).parent / "data" / "server.db"


def _ensure_db_dir():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    _ensure_db_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database():
    """Create all tables if they don't exist."""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT,
                settings_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS subscription_plans (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price_usdt REAL NOT NULL,
                duration_days INTEGER NOT NULL,
                features_json TEXT DEFAULT '[]',
                is_active INTEGER DEFAULT 1,
                max_signals_per_day INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                start_date TEXT,
                end_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (plan_id) REFERENCES subscription_plans(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                subscription_id TEXT,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'USDT',
                network TEXT DEFAULT 'TRC20',
                tx_hash TEXT DEFAULT '',
                wallet_address TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                confirmed_at TEXT,
                expires_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
            );

            CREATE TABLE IF NOT EXISTS admin_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
        """)
        conn.commit()
        logger.info("[Database] Tables initialized")

        # Seed default admin if none exists
        _seed_defaults(conn)
    finally:
        conn.close()


def _seed_defaults(conn):
    """Seed default admin user and subscription plans."""
    # Check if admin exists
    row = conn.execute("SELECT * FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not row:
        from auth import hash_password
        admin_id = str(uuid.uuid4())
        admin_username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin").lower().strip()
        admin_email = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost").lower().strip()
        admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD") or secrets.token_urlsafe(18)
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?,?,?,?,?)",
            (admin_id, admin_username, admin_email, hash_password(admin_password), "admin")
        )
        if os.getenv("DEFAULT_ADMIN_PASSWORD"):
            logger.info(f"[Database] Default admin created (username: {admin_username}, password from DEFAULT_ADMIN_PASSWORD)")
        else:
            logger.warning(
                f"[Database] Default admin created (username: {admin_username}, "
                f"temporary password: {admin_password})"
            )
    elif row["username"] == "admin":
        from auth import hash_password, verify_password
        if verify_password("admin123", row["password_hash"]):
            admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD") or secrets.token_urlsafe(18)
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(admin_password), row["id"]),
            )
            if os.getenv("DEFAULT_ADMIN_PASSWORD"):
                logger.warning("[Database] Rotated legacy admin/admin123 password to DEFAULT_ADMIN_PASSWORD")
            else:
                logger.warning(
                    "[Database] Rotated legacy admin/admin123 password. "
                    f"Temporary admin password: {admin_password}"
                )

    # Seed default plans if none exist
    plan_count = conn.execute("SELECT COUNT(*) FROM subscription_plans").fetchone()[0]
    if plan_count == 0:
        plans = [
            (str(uuid.uuid4()), "Free Trial", "7-day free trial with limited signals", 0.0, 7, '["5 signals/day","Basic AI analysis"]', 1, 5),
            (str(uuid.uuid4()), "Basic Monthly", "Standard monthly plan", 29.99, 30, '["Unlimited signals","Full AI analysis","Email support"]', 1, 0),
            (str(uuid.uuid4()), "Pro Monthly", "Professional monthly plan", 79.99, 30, '["Unlimited signals","Full AI analysis","Multi-TP & Trailing Stop","Priority support","Custom AI prompts"]', 1, 0),
            (str(uuid.uuid4()), "Pro Yearly", "Professional yearly plan (save 30%)", 599.99, 365, '["Everything in Pro Monthly","30% discount","Dedicated support"]', 1, 0),
        ]
        conn.executemany(
            "INSERT INTO subscription_plans (id, name, description, price_usdt, duration_days, features_json, is_active, max_signals_per_day) VALUES (?,?,?,?,?,?,?,?)",
            plans,
        )
        logger.info("[Database] Default subscription plans created")

    conn.commit()


# ─────────────────────────────────────────────
# User CRUD
# ─────────────────────────────────────────────
def create_user(username: str, email: str, password_hash: str, role: str = "user") -> dict:
    conn = get_connection()
    try:
        user_id = str(uuid.uuid4())
        normalized_username = username.lower().strip()
        normalized_email = email.lower().strip()
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?,?,?,?,?)",
            (user_id, normalized_username, normalized_email, password_hash, role),
        )
        conn.commit()
        return {"id": user_id, "username": normalized_username, "email": normalized_email, "role": role}
    except sqlite3.IntegrityError as e:
        if "username" in str(e):
            raise ValueError("Username already exists")
        elif "email" in str(e):
            raise ValueError("Email already registered")
        raise
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.lower().strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_user_login(user_id: str):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (datetime.utcnow().isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()


def get_all_users() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, email, role, is_active, created_at, last_login FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_status(user_id: str, is_active: bool):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, user_id))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Subscription Plans CRUD
# ─────────────────────────────────────────────
def get_subscription_plans(active_only: bool = True) -> list[dict]:
    conn = get_connection()
    try:
        if active_only:
            rows = conn.execute("SELECT * FROM subscription_plans WHERE is_active=1 ORDER BY price_usdt").fetchall()
        else:
            rows = conn.execute("SELECT * FROM subscription_plans ORDER BY price_usdt").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d.get("features_json", "[]"))
            result.append(d)
        return result
    finally:
        conn.close()


def create_subscription_plan(name: str, description: str, price: float, duration_days: int,
                              features: list[str], max_signals: int = 0) -> dict:
    conn = get_connection()
    try:
        plan_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO subscription_plans (id, name, description, price_usdt, duration_days, features_json, max_signals_per_day) VALUES (?,?,?,?,?,?,?)",
            (plan_id, name, description, price, duration_days, json.dumps(features), max_signals),
        )
        conn.commit()
        return {"id": plan_id, "name": name, "price_usdt": price}
    finally:
        conn.close()


def update_subscription_plan(plan_id: str, **kwargs):
    conn = get_connection()
    try:
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k == "features":
                sets.append("features_json=?")
                vals.append(json.dumps(v))
            elif k in ("name", "description", "price_usdt", "duration_days", "is_active", "max_signals_per_day"):
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            vals.append(plan_id)
            conn.execute(f"UPDATE subscription_plans SET {','.join(sets)} WHERE id=?", vals)
            conn.commit()
    finally:
        conn.close()


def delete_subscription_plan(plan_id: str):
    conn = get_connection()
    try:
        conn.execute("UPDATE subscription_plans SET is_active=0 WHERE id=?", (plan_id,))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Subscriptions CRUD
# ─────────────────────────────────────────────
def create_subscription(user_id: str, plan_id: str) -> dict:
    conn = get_connection()
    try:
        sub_id = str(uuid.uuid4())
        plan = conn.execute("SELECT * FROM subscription_plans WHERE id=? AND is_active=1", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("Plan not found")

        now = datetime.utcnow()
        end = now + timedelta(days=plan["duration_days"])

        conn.execute(
            "INSERT INTO subscriptions (id, user_id, plan_id, status, start_date, end_date) VALUES (?,?,?,?,?,?)",
            (sub_id, user_id, plan_id, "pending", now.isoformat(), end.isoformat()),
        )
        conn.commit()
        return {
            "id": sub_id,
            "plan_id": plan_id,
            "status": "pending",
            "end_date": end.isoformat(),
            "price_usdt": plan["price_usdt"],
            "plan_name": plan["name"],
        }
    finally:
        conn.close()


def activate_subscription(subscription_id: str):
    conn = get_connection()
    try:
        # Get subscription to recalculate end date from activation time
        sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
        if sub:
            plan = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (sub["plan_id"],)).fetchone()
            now = datetime.utcnow()
            end = now + timedelta(days=plan["duration_days"]) if plan else now + timedelta(days=30)
            conn.execute(
                "UPDATE subscriptions SET status='active', start_date=?, end_date=? WHERE id=?",
                (now.isoformat(), end.isoformat(), subscription_id),
            )
            conn.commit()
    finally:
        conn.close()


def get_user_active_subscription(user_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT s.*, p.name as plan_name, p.price_usdt, p.features_json, p.max_signals_per_day
            FROM subscriptions s
            JOIN subscription_plans p ON s.plan_id = p.id
            WHERE s.user_id=? AND s.status='active' AND datetime(s.end_date) > datetime('now')
            ORDER BY s.end_date DESC LIMIT 1
        """, (user_id,)).fetchone()
        if row:
            d = dict(row)
            d["features"] = json.loads(d.get("features_json", "[]"))
            return d
        return None
    finally:
        conn.close()


def get_user_subscriptions(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT s.*, p.name as plan_name, p.price_usdt
            FROM subscriptions s
            JOIN subscription_plans p ON s.plan_id = p.id
            WHERE s.user_id=?
            ORDER BY s.created_at DESC
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Payments CRUD
# ─────────────────────────────────────────────
def create_payment(user_id: str, subscription_id: str, amount: float,
                   currency: str = "USDT", network: str = "TRC20",
                   wallet_address: str = "") -> dict:
    conn = get_connection()
    try:
        payment_id = str(uuid.uuid4())
        expires_at = (datetime.utcnow() + timedelta(hours=24)).isoformat()
        conn.execute(
            """INSERT INTO payments (id, user_id, subscription_id, amount, currency, network,
               wallet_address, status, expires_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (payment_id, user_id, subscription_id, amount, currency, network,
             wallet_address, "pending", expires_at),
        )
        conn.commit()
        return {
            "id": payment_id, "amount": amount, "currency": currency,
            "network": network, "wallet_address": wallet_address,
            "status": "pending", "expires_at": expires_at,
        }
    finally:
        conn.close()


def confirm_payment(payment_id: str, tx_hash: str = ""):
    conn = get_connection()
    try:
        existing = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not existing:
            raise ValueError("Payment not found")
        final_tx_hash = tx_hash or existing["tx_hash"] or ""
        conn.execute(
            "UPDATE payments SET status='confirmed', tx_hash=?, confirmed_at=? WHERE id=?",
            (final_tx_hash, datetime.utcnow().isoformat(), payment_id),
        )
        # Get the subscription and activate it
        if existing["subscription_id"]:
            sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (existing["subscription_id"],)).fetchone()
            if sub:
                plan = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (sub["plan_id"],)).fetchone()
                now = datetime.utcnow()
                end = now + timedelta(days=plan["duration_days"]) if plan else now + timedelta(days=30)
                conn.execute(
                    "UPDATE subscriptions SET status='active', start_date=?, end_date=? WHERE id=?",
                    (now.isoformat(), end.isoformat(), existing["subscription_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def get_pending_payment_for_subscription(
    user_id: str,
    subscription_id: str,
    currency: str,
    network: str,
) -> dict | None:
    """Return the newest pending/submitted payment for a subscription, if any."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM payments
            WHERE user_id=? AND subscription_id=? AND currency=? AND network=?
              AND status IN ('pending', 'submitted')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, subscription_id, currency, network),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def submit_payment_tx(payment_id: str, user_id: str, tx_hash: str) -> bool:
    """Attach a tx hash to a user's payment and mark it ready for admin review."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE payments SET tx_hash=?, status='submitted' WHERE id=? AND user_id=? AND status IN ('pending', 'submitted')",
            (tx_hash, payment_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user_payments(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_payments(status: str = None) -> list[dict]:
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT p.*, u.username FROM payments p LEFT JOIN users u ON p.user_id=u.id WHERE p.status=? ORDER BY p.created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT p.*, u.username FROM payments p LEFT JOIN users u ON p.user_id=u.id ORDER BY p.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Admin settings
# ─────────────────────────────────────────────
def get_admin_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM admin_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_admin_setting(key: str, value: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value, updated_at) VALUES (?,?,?)",
            (key, value, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

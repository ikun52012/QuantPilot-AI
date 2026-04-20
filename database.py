"""
TradingView Signal Server - Database Layer
SQLite database for users, subscriptions, and payments.
"""
import sqlite3
import json
import uuid
import os
import secrets
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from security import decrypt_settings_payload, decrypt_value, encrypt_settings_payload, encrypt_value

DB_PATH = Path(__file__).parent / "data" / "server.db"
ADMIN_SENSITIVE_SETTINGS = {"webhook_secret"}


def _webhook_secret_hash(secret: str) -> str:
    secret = str(secret or "").strip()
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


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
                balance_usdt REAL DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT,
                settings_json TEXT DEFAULT '{}',
                webhook_secret TEXT DEFAULT '',
                webhook_secret_hash TEXT DEFAULT ''
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

            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                note TEXT DEFAULT '',
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                created_by TEXT,
                last_used_by TEXT,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                plan_id TEXT,
                duration_days INTEGER DEFAULT 0,
                balance_usdt REAL DEFAULT 0,
                note TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                redeemed_by TEXT,
                redeemed_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                created_by TEXT,
                FOREIGN KEY (plan_id) REFERENCES subscription_plans(id),
                FOREIGN KEY (redeemed_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                timestamp TEXT NOT NULL,
                ticker TEXT DEFAULT '',
                direction TEXT DEFAULT '',
                execute INTEGER DEFAULT 0,
                order_status TEXT DEFAULT '',
                pnl_pct REAL DEFAULT 0,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhook_events (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                fingerprint TEXT NOT NULL,
                ticker TEXT DEFAULT '',
                direction TEXT DEFAULT '',
                status TEXT NOT NULL,
                status_code INTEGER DEFAULT 200,
                reason TEXT DEFAULT '',
                client_ip TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id TEXT PRIMARY KEY,
                admin_id TEXT,
                admin_username TEXT DEFAULT '',
                action TEXT NOT NULL,
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                client_ip TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_webhook_secret ON users(webhook_secret);
            CREATE INDEX IF NOT EXISTS idx_users_webhook_secret_hash ON users(webhook_secret_hash);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status, end_date);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_sub ON payments(subscription_id, status);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_invite_codes_active ON invite_codes(is_active);
            CREATE INDEX IF NOT EXISTS idx_redeem_codes_active ON redeem_codes(is_active);
            CREATE INDEX IF NOT EXISTS idx_trades_user_time ON trades(user_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_webhook_fingerprint_time ON webhook_events(fingerprint, created_at);
            CREATE INDEX IF NOT EXISTS idx_webhook_user_time ON webhook_events(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit_logs(created_at);
        """)
        _migrate_schema(conn)
        conn.commit()
        logger.info("[Database] Tables initialized")

        # Seed default admin if none exists
        _seed_defaults(conn)
    finally:
        conn.close()


def _migrate_schema(conn):
    """Apply additive migrations for older local SQLite databases."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    migrations = [
        ("balance_usdt", "ALTER TABLE users ADD COLUMN balance_usdt REAL DEFAULT 0"),
        ("webhook_secret", "ALTER TABLE users ADD COLUMN webhook_secret TEXT DEFAULT ''"),
        ("webhook_secret_hash", "ALTER TABLE users ADD COLUMN webhook_secret_hash TEXT DEFAULT ''"),
    ]
    for col, sql in migrations:
        if col not in columns:
            conn.execute(sql)
    # Backfill webhook_secret_hash from legacy plaintext columns or settings_json.
    rows = conn.execute(
        "SELECT id, settings_json, webhook_secret FROM users WHERE webhook_secret_hash IS NULL OR webhook_secret_hash = ''"
    ).fetchall()
    for row in rows:
        try:
            s = decrypt_settings_payload(json.loads(row["settings_json"] or "{}"))
            secret = str(row["webhook_secret"] or "") or s.get("webhook", {}).get("secret", "")
            if secret:
                conn.execute(
                    "UPDATE users SET webhook_secret_hash=?, webhook_secret='' WHERE id=?",
                    (_webhook_secret_hash(secret), row["id"]),
                )
        except Exception:
            pass
    # Opportunistically encrypt existing per-user sensitive settings.
    rows = conn.execute("SELECT id, settings_json FROM users WHERE settings_json IS NOT NULL AND settings_json <> '{}'").fetchall()
    for row in rows:
        try:
            settings = json.loads(row["settings_json"] or "{}")
            encrypted = encrypt_settings_payload(decrypt_settings_payload(settings))
            encoded = json.dumps(encrypted, ensure_ascii=False)
            if encoded != (row["settings_json"] or "{}"):
                conn.execute("UPDATE users SET settings_json=? WHERE id=?", (encoded, row["id"]))
        except Exception:
            pass
    # Encrypt sensitive admin settings, including the global webhook secret.
    rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
    for row in rows:
        if row["key"] in ADMIN_SENSITIVE_SETTINGS:
            encrypted = encrypt_value(decrypt_value(row["value"] or ""))
            if encrypted != (row["value"] or ""):
                conn.execute(
                    "UPDATE admin_settings SET value=?, updated_at=? WHERE key=?",
                    (encrypted, datetime.utcnow().isoformat(), row["key"]),
                )


def _seed_defaults(conn):
    """Seed default admin user and subscription plans."""
    # Check if admin exists
    row = conn.execute("SELECT * FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not row:
        from auth import hash_password
        admin_id = str(uuid.uuid4())
        admin_username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin").lower().strip()
        admin_email = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost").lower().strip()
        admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "123456")
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?,?,?,?,?)",
            (admin_id, admin_username, admin_email, hash_password(admin_password), "admin")
        )
        if admin_password == "123456":
            logger.warning(f"[Database] Default admin created (username: {admin_username}, password: 123456). Change it after first login.")
        else:
            logger.info(f"[Database] Default admin created (username: {admin_username}, password from DEFAULT_ADMIN_PASSWORD)")
    elif row["username"] == "admin":
        from auth import hash_password, verify_password
        if verify_password("admin123", row["password_hash"]):
            admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "123456")
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(admin_password), row["id"]),
            )
            if admin_password == "123456":
                logger.warning("[Database] Rotated legacy admin/admin123 password to 123456. Change it after first login.")
            else:
                logger.warning("[Database] Rotated legacy admin/admin123 password to DEFAULT_ADMIN_PASSWORD")

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
            "SELECT id, username, email, role, balance_usdt, is_active, created_at, last_login FROM users ORDER BY created_at DESC"
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


def update_user_password_hash(user_id: str, password_hash: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_user_admin(
    user_id: str,
    username: str,
    email: str,
    role: str,
    is_active: bool,
    balance_usdt: float,
) -> dict:
    """Admin edit for account profile and balance."""
    conn = get_connection()
    try:
        username = username.lower().strip()
        email = email.lower().strip()
        if role not in ("user", "admin"):
            raise ValueError("Invalid role")
        conn.execute(
            """
            UPDATE users
            SET username=?, email=?, role=?, is_active=?, balance_usdt=?
            WHERE id=?
            """,
            (username, email, role, 1 if is_active else 0, balance_usdt, user_id),
        )
        conn.commit()
        row = conn.execute("SELECT id, username, email, role, balance_usdt, is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found")
        return dict(row)
    except sqlite3.IntegrityError as e:
        if "username" in str(e):
            raise ValueError("Username already exists")
        if "email" in str(e):
            raise ValueError("Email already registered")
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Subscription Plans CRUD
# ─────────────────────────────────────────────
def create_user_admin(
    username: str,
    email: str,
    password_hash: str,
    role: str = "user",
    is_active: bool = True,
    balance_usdt: float = 0.0,
) -> dict:
    """Create a user from the admin panel with editable account fields."""
    conn = get_connection()
    try:
        if role not in ("user", "admin"):
            raise ValueError("Invalid role")
        user_id = str(uuid.uuid4())
        normalized_username = username.lower().strip()
        normalized_email = email.lower().strip()
        conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, role, is_active, balance_usdt)
            VALUES (?,?,?,?,?,?,?)
            """,
            (user_id, normalized_username, normalized_email, password_hash, role, 1 if is_active else 0, balance_usdt),
        )
        conn.commit()
        return {
            "id": user_id,
            "username": normalized_username,
            "email": normalized_email,
            "role": role,
            "is_active": 1 if is_active else 0,
            "balance_usdt": balance_usdt,
        }
    except sqlite3.IntegrityError as e:
        if "username" in str(e):
            raise ValueError("Username already exists")
        if "email" in str(e):
            raise ValueError("Email already registered")
        raise
    finally:
        conn.close()


def delete_user_admin(user_id: str) -> bool:
    """Hard-delete a user and their owned billing rows."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False
        if row["role"] == "admin":
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if admin_count <= 1:
                raise ValueError("Cannot delete the last admin account")
        conn.execute("UPDATE redeem_codes SET redeemed_by=NULL WHERE redeemed_by=?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user_settings(user_id: str) -> dict:
    conn = get_connection()
    try:
        row = conn.execute("SELECT settings_json FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {}
        try:
            return decrypt_settings_payload(json.loads(row["settings_json"] or "{}"))
        except json.JSONDecodeError:
            return {}
    finally:
        conn.close()


def update_user_settings(user_id: str, updates: dict) -> dict:
    """Deep-merge per-user settings into users.settings_json."""
    current = get_user_settings(user_id)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE users SET settings_json=? WHERE id=?",
            (json.dumps(encrypt_settings_payload(current), ensure_ascii=False), user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("User not found")
        return current
    finally:
        conn.close()


def ensure_user_webhook_secret(user_id: str) -> str:
    settings = get_user_settings(user_id)
    secret = (settings.get("webhook") or {}).get("secret", "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        update_user_settings(user_id, {"webhook": {"secret": secret}})
    # Store only a hash in the indexed column for O(1) lookup without plaintext at rest.
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET webhook_secret_hash=?, webhook_secret='' WHERE id=?",
            (_webhook_secret_hash(secret), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return secret


def find_user_by_webhook_secret(secret: str) -> dict | None:
    """O(1) lookup via indexed webhook secret hash."""
    secret = str(secret or "").strip()
    if not secret:
        return None
    secret_hash = _webhook_secret_hash(secret)
    conn = get_connection()
    try:
        # Fast indexed lookup first. The plaintext legacy column is intentionally not used for new writes.
        row = conn.execute(
            "SELECT id, username, email, role, is_active, webhook_secret_hash FROM users "
            "WHERE is_active=1 AND webhook_secret_hash=?",
            (secret_hash,),
        ).fetchone()
        if row and secrets.compare_digest(str(row["webhook_secret_hash"]), secret_hash):
            user = dict(row)
            user.pop("webhook_secret_hash", None)
            return user
        # Legacy fallback for databases that still have plaintext webhook_secret.
        rows = conn.execute(
            "SELECT id, username, email, role, is_active, webhook_secret FROM users "
            "WHERE is_active=1 AND webhook_secret IS NOT NULL AND webhook_secret <> ''"
        ).fetchall()
        for row in rows:
            stored = str(row["webhook_secret"] or "")
            if stored and secrets.compare_digest(stored, secret):
                conn.execute(
                    "UPDATE users SET webhook_secret_hash=?, webhook_secret='' WHERE id=?",
                    (secret_hash, row["id"]),
                )
                conn.commit()
                user = dict(row)
                user.pop("webhook_secret", None)
                return user
        # Fallback: scan encrypted settings_json for users whose hash was not backfilled yet.
        rows = conn.execute(
            "SELECT id, username, email, role, is_active, settings_json FROM users "
            "WHERE is_active=1 AND (webhook_secret_hash IS NULL OR webhook_secret_hash = '')"
        ).fetchall()
        for row in rows:
            try:
                user_settings = decrypt_settings_payload(json.loads(row["settings_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            stored = str(user_settings.get("webhook", {}).get("secret", ""))
            if stored and secrets.compare_digest(stored, str(secret)):
                conn.execute(
                    "UPDATE users SET webhook_secret_hash=?, webhook_secret='' WHERE id=?",
                    (secret_hash, row["id"]),
                )
                conn.commit()
                user = dict(row)
                user.pop("settings_json", None)
                return user
        return None
    finally:
        conn.close()


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


def pay_subscription_from_balance(user_id: str, subscription_id: str, amount: float) -> dict | None:
    """Activate a subscription by deducting the user's internal USDT balance."""
    conn = get_connection()
    try:
        user = conn.execute("SELECT balance_usdt FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise ValueError("User not found")
        balance = float(user["balance_usdt"] or 0)
        amount = float(amount or 0)
        if amount <= 0 or balance + 1e-9 < amount:
            return None

        sub = conn.execute(
            """
            SELECT s.*, p.duration_days
            FROM subscriptions s
            JOIN subscription_plans p ON s.plan_id=p.id
            WHERE s.id=? AND s.user_id=?
            """,
            (subscription_id, user_id),
        ).fetchone()
        if not sub:
            raise ValueError("Subscription not found")

        now = datetime.utcnow()
        end = now + timedelta(days=sub["duration_days"])
        new_balance = balance - amount
        conn.execute("UPDATE users SET balance_usdt=? WHERE id=?", (new_balance, user_id))
        conn.execute(
            "UPDATE subscriptions SET status='active', start_date=?, end_date=? WHERE id=?",
            (now.isoformat(), end.isoformat(), subscription_id),
        )
        conn.commit()
        return {
            "subscription_id": subscription_id,
            "balance_usdt": new_balance,
            "end_date": end.isoformat(),
        }
    finally:
        conn.close()


def set_user_subscription(
    user_id: str,
    plan_id: str,
    status: str = "active",
    duration_days: int | None = None,
) -> dict:
    """Create a new subscription for a user, usually from admin or redeem code."""
    conn = get_connection()
    try:
        plan = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("Plan not found")
        now = datetime.utcnow()
        days = duration_days if duration_days and duration_days > 0 else plan["duration_days"]
        end = now + timedelta(days=days)
        sub_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO subscriptions (id, user_id, plan_id, status, start_date, end_date)
            VALUES (?,?,?,?,?,?)
            """,
            (sub_id, user_id, plan_id, status, now.isoformat(), end.isoformat()),
        )
        conn.commit()
        return {"id": sub_id, "user_id": user_id, "plan_id": plan_id, "status": status, "end_date": end.isoformat()}
    finally:
        conn.close()


def adjust_user_balance(user_id: str, delta_usdt: float) -> float:
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET balance_usdt=COALESCE(balance_usdt,0)+? WHERE id=?", (delta_usdt, user_id))
        conn.commit()
        row = conn.execute("SELECT balance_usdt FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found")
        return float(row["balance_usdt"] or 0)
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


def payment_tx_hash_exists(tx_hash: str, exclude_payment_id: str = "") -> bool:
    tx_hash = (tx_hash or "").strip()
    if not tx_hash:
        return False
    conn = get_connection()
    try:
        if exclude_payment_id:
            row = conn.execute(
                "SELECT id FROM payments WHERE LOWER(tx_hash)=LOWER(?) AND id<>? LIMIT 1",
                (tx_hash, exclude_payment_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT id FROM payments WHERE LOWER(tx_hash)=LOWER(?) LIMIT 1", (tx_hash,)).fetchone()
        return row is not None
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
        value = row["value"] if row else default
        if key in ADMIN_SENSITIVE_SETTINGS:
            return decrypt_value(value)
        return value
    finally:
        conn.close()


def set_admin_setting(key: str, value: str):
    stored_value = encrypt_value(value) if key in ADMIN_SENSITIVE_SETTINGS else value
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value, updated_at) VALUES (?,?,?)",
            (key, stored_value, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Trade, webhook, and audit logs
# ─────────────────────────────────────────────
def insert_trade_log(entry: dict):
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO trades
                (id, user_id, timestamp, ticker, direction, execute, order_status, pnl_pct, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                entry.get("id"),
                entry.get("user_id"),
                entry.get("timestamp") or datetime.utcnow().isoformat(),
                entry.get("ticker", ""),
                entry.get("direction", ""),
                1 if entry.get("execute") else 0,
                entry.get("order_status", ""),
                float(entry.get("pnl_pct") or 0.0),
                json.dumps(entry, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_trade_logs(days: int = 30, user_id: str | None = None) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=max(1, min(int(days), 365)) - 1)).strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        if user_id is None:
            rows = conn.execute(
                "SELECT payload_json FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT payload_json FROM trades WHERE user_id=? AND timestamp >= ? ORDER BY timestamp DESC",
                (user_id, since),
            ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(json.loads(row["payload_json"]))
            except json.JSONDecodeError:
                continue
        return result
    finally:
        conn.close()


def count_today_executed_trades(user_id: str | None = None) -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    statuses = ("filled", "simulated", "closed")
    conn = get_connection()
    try:
        if user_id is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE timestamp >= ? AND execute=1 AND order_status IN (?,?,?)",
                (today, *statuses),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE user_id=? AND timestamp >= ? AND execute=1 AND order_status IN (?,?,?)",
                (user_id, today, *statuses),
            ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def record_webhook_event(
    fingerprint: str,
    status: str,
    status_code: int = 200,
    user_id: str | None = None,
    ticker: str = "",
    direction: str = "",
    reason: str = "",
    client_ip: str = "",
    payload: dict | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO webhook_events
                (id, user_id, fingerprint, ticker, direction, status, status_code, reason, client_ip, payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                user_id,
                fingerprint,
                ticker,
                direction,
                status,
                status_code,
                reason,
                client_ip,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        return event_id
    finally:
        conn.close()


def has_recent_webhook_event(fingerprint: str, minutes: int = 10) -> bool:
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id FROM webhook_events
            WHERE fingerprint=? AND status IN ('processed', 'blocked', 'rejected', 'executed')
              AND datetime(created_at) >= datetime(?)
            LIMIT 1
            """,
            (fingerprint, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_admin_audit_log(
    admin_id: str,
    admin_username: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    summary: str = "",
    client_ip: str = "",
) -> str:
    audit_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO admin_audit_logs
                (id, admin_id, admin_username, action, target_type, target_id, summary, client_ip)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (audit_id, admin_id, admin_username, action, target_type, target_id, summary, client_ip),
        )
        conn.commit()
        return audit_id
    finally:
        conn.close()


def get_admin_audit_logs(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM admin_audit_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Invite and redeem codes
# ─────────────────────────────────────────────
def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.utcnow()
    except ValueError:
        return True


def create_invite_code(note: str = "", max_uses: int = 1, expires_at: str = "", created_by: str = "") -> dict:
    conn = get_connection()
    try:
        code = "INV-" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].upper()
        conn.execute(
            """
            INSERT INTO invite_codes (code, note, max_uses, expires_at, created_by)
            VALUES (?,?,?,?,?)
            """,
            (code, note, max_uses, expires_at or None, created_by),
        )
        conn.commit()
        return {"code": code, "note": note, "max_uses": max_uses, "expires_at": expires_at}
    finally:
        conn.close()


def list_invite_codes(limit: int = 100) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def validate_and_consume_invite(code: str, user_id: str) -> bool:
    code = code.strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not row:
            return False
        if not row["is_active"] or row["used_count"] >= row["max_uses"]:
            return False
        if _is_expired(row["expires_at"]):
            return False
        cur = conn.execute(
            """
            UPDATE invite_codes
            SET used_count=used_count+1, last_used_by=?, last_used_at=?,
                is_active=CASE WHEN used_count+1 >= max_uses THEN 0 ELSE is_active END
            WHERE code=? AND is_active=1 AND used_count < max_uses
            """,
            (user_id, datetime.utcnow().isoformat(), code),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_invite_code_valid(code: str) -> bool:
    code = code.strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not row:
            return False
        if not row["is_active"] or row["used_count"] >= row["max_uses"]:
            return False
        if _is_expired(row["expires_at"]):
            return False
        return True
    finally:
        conn.close()


def create_redeem_code(
    plan_id: str = "",
    duration_days: int = 0,
    balance_usdt: float = 0.0,
    note: str = "",
    expires_at: str = "",
    created_by: str = "",
) -> dict:
    conn = get_connection()
    try:
        if plan_id:
            plan = conn.execute("SELECT id FROM subscription_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise ValueError("Plan not found")
        code = "CARD-" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].upper()
        conn.execute(
            """
            INSERT INTO redeem_codes (code, plan_id, duration_days, balance_usdt, note, expires_at, created_by)
            VALUES (?,?,?,?,?,?,?)
            """,
            (code, plan_id or None, duration_days, balance_usdt, note, expires_at or None, created_by),
        )
        conn.commit()
        return {
            "code": code,
            "plan_id": plan_id,
            "duration_days": duration_days,
            "balance_usdt": balance_usdt,
            "note": note,
            "expires_at": expires_at,
        }
    finally:
        conn.close()


def list_redeem_codes(limit: int = 100) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.*, u.username AS redeemed_by_username, p.name AS plan_name
            FROM redeem_codes r
            LEFT JOIN users u ON r.redeemed_by=u.id
            LEFT JOIN subscription_plans p ON r.plan_id=p.id
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def redeem_code_for_user(code: str, user_id: str) -> dict:
    code = code.strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
        if not row:
            raise ValueError("Invalid redeem code")
        if not row["is_active"] or row["redeemed_by"]:
            raise ValueError("Redeem code has already been used")
        if _is_expired(row["expires_at"]):
            raise ValueError("Redeem code has expired")

        result = {"code": code, "balance_usdt": row["balance_usdt"] or 0, "subscription": None}
        if row["balance_usdt"]:
            conn.execute(
                "UPDATE users SET balance_usdt=COALESCE(balance_usdt,0)+? WHERE id=?",
                (row["balance_usdt"], user_id),
            )

        if row["plan_id"]:
            plan = conn.execute("SELECT * FROM subscription_plans WHERE id=?", (row["plan_id"],)).fetchone()
            if not plan:
                raise ValueError("Redeem code plan no longer exists")
            now = datetime.utcnow()
            days = row["duration_days"] or plan["duration_days"]
            end = now + timedelta(days=days)
            sub_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO subscriptions (id, user_id, plan_id, status, start_date, end_date)
                VALUES (?,?,?,?,?,?)
                """,
                (sub_id, user_id, row["plan_id"], "active", now.isoformat(), end.isoformat()),
            )
            result["subscription"] = {"id": sub_id, "plan_id": row["plan_id"], "end_date": end.isoformat()}

        conn.execute(
            "UPDATE redeem_codes SET is_active=0, redeemed_by=?, redeemed_at=? WHERE code=?",
            (user_id, datetime.utcnow().isoformat(), code),
        )
        conn.commit()
        return result
    finally:
        conn.close()

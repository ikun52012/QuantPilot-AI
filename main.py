"""
TradingView Signal Server v4.0 - Main Application

Complete pipeline:
  TradingView Webhook → Pre-Filter → AI Analysis → Trade Execution → Notification

Features:
  - User auth (JWT) with admin/user roles
  - Subscription system with crypto payments
  - Homepage, dashboard, login/register pages
  - Enhanced pre-filter (15 checks)
  - Multi-TP, trailing stop, custom AI

Usage:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import sys
import json
import hmac
import threading
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from typing import Optional

from config import settings
from models import (
    TradingViewSignal,
    TradeDecision,
    SignalDirection,
    TakeProfitLevel,
    TrailingStopConfig,
    TrailingStopMode,
)
from pre_filter import run_pre_filter, increment_trade_count
from ai_analyzer import analyze_signal
from market_data import fetch_market_context
from exchange import (
    execute_trade,
    get_account_balance,
    get_open_positions,
    get_recent_orders,
    test_exchange_connection,
    get_supported_exchanges,
)
from notifier import (
    notify_signal_received,
    notify_pre_filter_blocked,
    notify_ai_analysis,
    notify_trade_executed,
    notify_error,
    send_telegram,
)
from trade_logger import log_trade, get_today_stats, get_today_trades, get_trade_history
from analytics import calculate_performance, get_daily_pnl, get_trade_distribution, invalidate_performance_cache
from auth import (
    hash_password,
    verify_password,
    create_token,
    set_auth_cookie,
    clear_auth_cookie,
    get_current_user,
    require_admin,
    get_optional_user,
)
from database import (
    init_database,
    create_user,
    get_user_by_username,
    get_user_by_email,
    get_user_by_id,
    update_user_login,
    get_all_users,
    update_user_admin,
    update_user_status,
    pay_subscription_from_balance,
    set_user_subscription,
    get_subscription_plans,
    create_subscription_plan,
    update_subscription_plan,
    delete_subscription_plan,
    create_subscription,
    activate_subscription,
    get_user_active_subscription,
    get_user_subscriptions,
    create_payment as db_create_payment,
    confirm_payment,
    get_pending_payment_for_subscription,
    submit_payment_tx,
    get_user_payments,
    get_all_payments,
    get_admin_setting,
    set_admin_setting,
    create_invite_code,
    list_invite_codes,
    is_invite_code_valid,
    validate_and_consume_invite,
    create_redeem_code,
    list_redeem_codes,
    redeem_code_for_user,
)
from payment import (
    get_payment_address,
    set_payment_address,
    get_all_payment_addresses,
    create_payment_request,
    get_supported_payment_options,
    SUPPORTED_NETWORKS,
)

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add("logs/server_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days", level="DEBUG")

# Settings file for runtime config changes
SETTINGS_FILE = Path(__file__).parent / "runtime_settings.json"
_settings_lock = threading.Lock()


def _load_runtime_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_runtime_settings(data: dict):
    with _settings_lock:
        current = _load_runtime_settings()
        current.update(data)
        tmp_path = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(SETTINGS_FILE)


# ─────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database
    init_database()

    logger.info("=" * 50)
    logger.info("📡 TradingView Signal Server v4.0 starting...")
    logger.info(f"   AI Provider: {settings.ai.provider}")
    logger.info(f"   Exchange: {settings.exchange.name}")
    logger.info(f"   Live Trading: {'🔴 YES' if settings.exchange.live_trading else '🟢 NO (Paper)'}")
    logger.info(f"   Supported Exchanges: {', '.join(get_supported_exchanges())}")
    logger.info(f"   TP Levels: {settings.take_profit.num_levels}")
    logger.info(f"   Trailing Stop: {settings.trailing_stop.mode}")
    logger.info("=" * 50)

    # Apply runtime settings on startup
    rs = _load_runtime_settings()
    if rs.get("exchange"):
        settings.exchange.name = rs["exchange"].get("name", settings.exchange.name)
    if rs.get("ai"):
        settings.ai.provider = rs["ai"].get("provider", settings.ai.provider)
        settings.ai.temperature = rs["ai"].get("temperature", settings.ai.temperature)
        settings.ai.max_tokens = rs["ai"].get("max_tokens", settings.ai.max_tokens)
        settings.ai.custom_system_prompt = rs["ai"].get("custom_system_prompt", settings.ai.custom_system_prompt)
        settings.ai.custom_provider_enabled = rs["ai"].get("custom_provider_enabled", settings.ai.custom_provider_enabled)
        settings.ai.custom_provider_name = rs["ai"].get("custom_provider_name", settings.ai.custom_provider_name)
        settings.ai.custom_provider_model = rs["ai"].get("custom_provider_model", settings.ai.custom_provider_model)
        settings.ai.custom_provider_api_url = rs["ai"].get("custom_provider_api_url", settings.ai.custom_provider_api_url)
    if rs.get("telegram"):
        settings.telegram.chat_id = rs["telegram"].get("chat_id", settings.telegram.chat_id)
    if rs.get("risk"):
        settings.risk.account_equity_usdt = rs["risk"].get("account_equity_usdt", settings.risk.account_equity_usdt)
        settings.risk.max_position_pct = rs["risk"].get("max_position_pct", settings.risk.max_position_pct)
        settings.risk.max_daily_trades = rs["risk"].get("max_daily_trades", settings.risk.max_daily_trades)
        settings.risk.max_daily_loss_pct = rs["risk"].get("max_daily_loss_pct", settings.risk.max_daily_loss_pct)
        settings.risk.exit_management_mode = rs["risk"].get("exit_management_mode", settings.risk.exit_management_mode)
        settings.risk.custom_stop_loss_pct = rs["risk"].get("custom_stop_loss_pct", settings.risk.custom_stop_loss_pct)
        settings.risk.ai_exit_system_prompt = rs["risk"].get("ai_exit_system_prompt", settings.risk.ai_exit_system_prompt)
    if rs.get("take_profit"):
        tp = rs["take_profit"]
        settings.take_profit.num_levels = tp.get("num_levels", settings.take_profit.num_levels)
        settings.take_profit.tp1_pct = tp.get("tp1_pct", settings.take_profit.tp1_pct)
        settings.take_profit.tp2_pct = tp.get("tp2_pct", settings.take_profit.tp2_pct)
        settings.take_profit.tp3_pct = tp.get("tp3_pct", settings.take_profit.tp3_pct)
        settings.take_profit.tp4_pct = tp.get("tp4_pct", settings.take_profit.tp4_pct)
        settings.take_profit.tp1_qty = tp.get("tp1_qty", settings.take_profit.tp1_qty)
        settings.take_profit.tp2_qty = tp.get("tp2_qty", settings.take_profit.tp2_qty)
        settings.take_profit.tp3_qty = tp.get("tp3_qty", settings.take_profit.tp3_qty)
        settings.take_profit.tp4_qty = tp.get("tp4_qty", settings.take_profit.tp4_qty)
    if rs.get("trailing_stop"):
        ts = rs["trailing_stop"]
        settings.trailing_stop.mode = ts.get("mode", settings.trailing_stop.mode)
        settings.trailing_stop.trail_pct = ts.get("trail_pct", settings.trailing_stop.trail_pct)
        settings.trailing_stop.activation_profit_pct = ts.get("activation_profit_pct", settings.trailing_stop.activation_profit_pct)
        settings.trailing_stop.trailing_step_pct = ts.get("trailing_step_pct", settings.trailing_stop.trailing_step_pct)

    if settings.exchange.live_trading and not settings.server.webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET must be set when LIVE_TRADING=true")

    yield
    logger.info("TradingView Signal Server shutting down...")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────
app = FastAPI(
    title="TradingView Signal Server",
    description="AI-powered crypto trading signal processor with subscriptions",
    version="4.0.0",
    lifespan=lifespan,
)

# Mount static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def homepage():
    return FileResponse(STATIC_DIR / "home.html")

@app.get("/dashboard")
async def dashboard(request: Request):
    if not get_optional_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")

@app.get("/register")
async def register_page():
    return FileResponse(STATIC_DIR / "register.html")


# ═══════════════════════════════════════════════
# AUTH API
# ═══════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    invite_code: str = Field(default="", max_length=80)

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class SubscribeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=80)


class PaymentCreateRequest(BaseModel):
    subscription_id: str = Field(min_length=1, max_length=80)
    currency: str = Field(default="USDT", min_length=2, max_length=12)
    network: str = Field(default="TRC20", min_length=2, max_length=20)


class PaymentSubmitRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=80)
    tx_hash: str = Field(min_length=6, max_length=200)


class RedeemCodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=80)


@app.post("/api/auth/register")
async def api_register(req: RegisterRequest, response: Response):
    username = req.username.lower().strip()
    email = req.email.lower().strip()
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(400, "Invalid email address")

    # Check if username or email already taken
    if get_user_by_username(username):
        raise HTTPException(400, "Username already exists")
    if get_user_by_email(email):
        raise HTTPException(400, "Email already registered")
    invite_required = get_admin_setting("registration_invite_required", "false").lower() == "true"
    invite_code = req.invite_code.strip().upper()
    if invite_required and not invite_code:
        raise HTTPException(400, "Invite code is required")
    if invite_required and not is_invite_code_valid(invite_code):
        raise HTTPException(400, "Invalid or expired invite code")

    pw_hash = hash_password(req.password)
    try:
        user = create_user(username, email, pw_hash)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if invite_required and not validate_and_consume_invite(invite_code, user["id"]):
        update_user_status(user["id"], False)
        raise HTTPException(400, "Invalid or expired invite code")
    token = create_token(user["id"], user["username"], user["role"])
    set_auth_cookie(response, token)

    logger.info(f"[Auth] New user registered: {username}")
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "email": email, "role": user["role"]}}


@app.post("/api/auth/login")
async def api_login(req: LoginRequest, response: Response):
    username = req.username.lower().strip()
    user = get_user_by_username(username)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")

    if not user.get("is_active", 1):
        raise HTTPException(403, "Account is disabled")

    update_user_login(user["id"])
    token = create_token(user["id"], user["username"], user["role"])
    set_auth_cookie(response, token)

    logger.info(f"[Auth] User logged in: {username}")
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/logout")
async def api_logout(response: Response):
    clear_auth_cookie(response)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def api_me(user=Depends(get_current_user)):
    db_user = get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(404, "User not found")

    sub = get_user_active_subscription(user["sub"])
    return {
        "id": db_user["id"],
        "username": db_user["username"],
        "email": db_user["email"],
        "role": db_user["role"],
        "balance_usdt": db_user.get("balance_usdt", 0),
        "created_at": db_user["created_at"],
        "subscription": sub,
    }


# ═══════════════════════════════════════════════
# SUBSCRIPTION API
# ═══════════════════════════════════════════════

@app.get("/api/plans")
async def api_get_plans():
    """Get all active subscription plans (public)."""
    return get_subscription_plans(active_only=True)


@app.get("/api/registration-settings")
async def api_registration_settings():
    return {
        "invite_required": get_admin_setting("registration_invite_required", "false").lower() == "true"
    }


@app.post("/api/subscribe")
async def api_subscribe(req: SubscribeRequest, user=Depends(get_current_user)):
    """Create a subscription for the current user."""
    # Check if user already has active subscription
    current_sub = get_user_active_subscription(user["sub"])
    if current_sub:
        raise HTTPException(400, "You already have an active subscription")

    try:
        sub = create_subscription(user["sub"], req.plan_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if sub.get("price_usdt", 0) <= 0:
        activate_subscription(sub["id"])
        sub["status"] = "active"
        logger.info(f"[Subscription] Free plan activated for {user['username']}: {req.plan_id}")
        return sub

    db_user = get_user_by_id(user["sub"]) or {}
    if float(db_user.get("balance_usdt") or 0) >= float(sub.get("price_usdt") or 0):
        paid = pay_subscription_from_balance(user["sub"], sub["id"], sub["price_usdt"])
        if paid:
            sub["status"] = "active"
            sub["paid_from_balance"] = True
            sub["balance_usdt"] = paid["balance_usdt"]
            sub["end_date"] = paid["end_date"]
            logger.info(f"[Subscription] Balance paid for {user['username']}: {req.plan_id}")
            return sub

    logger.info(f"[Subscription] User {user['username']} subscribed to plan {req.plan_id}")
    return sub


@app.get("/api/my-subscription")
async def api_my_subscription(user=Depends(get_current_user)):
    sub = get_user_active_subscription(user["sub"])
    return sub or {"status": "none"}


@app.get("/api/my-subscriptions")
async def api_my_subscriptions(user=Depends(get_current_user)):
    return get_user_subscriptions(user["sub"])


# ═══════════════════════════════════════════════
# PAYMENT API
# ═══════════════════════════════════════════════

@app.get("/api/payment-options")
async def api_payment_options():
    """Get available payment networks and currencies."""
    networks = [n for n in get_supported_payment_options() if "USDT" in n.get("currencies", [])]
    return {
        "networks": networks,
        "supported": [key for key, value in SUPPORTED_NETWORKS.items() if "USDT" in value.get("currencies", [])],
    }


@app.post("/api/payment/create")
async def api_create_payment(req: PaymentCreateRequest, user=Depends(get_current_user)):
    """Create a payment for a subscription."""
    subscription_id = req.subscription_id
    currency = req.currency.upper().strip()
    network = req.network.upper().strip()

    # Get subscription to find the amount
    subs = get_user_subscriptions(user["sub"])
    sub = next((s for s in subs if s["id"] == subscription_id), None)
    if not sub:
        raise HTTPException(404, "Subscription not found")

    amount = sub.get("price_usdt", 0)
    if amount <= 0:
        # Free plan, activate immediately
        activate_subscription(subscription_id)
        return {"status": "activated", "message": "Free plan activated"}

    existing_payment = get_pending_payment_for_subscription(user["sub"], subscription_id, currency, network)

    # Get payment address
    payment_info = create_payment_request(user["sub"], amount, currency, network)
    if "error" in payment_info:
        raise HTTPException(400, payment_info["error"])

    if existing_payment:
        existing_payment.update(payment_info)
        if existing_payment.get("wallet_address"):
            existing_payment["address"] = existing_payment["wallet_address"]
        return existing_payment

    # Create payment record
    payment = db_create_payment(
        user_id=user["sub"],
        subscription_id=subscription_id,
        amount=amount,
        currency=currency,
        network=network,
        wallet_address=payment_info["address"],
    )

    payment.update(payment_info)
    logger.info(f"[Payment] Created for user {user['username']}: {amount} {currency} on {network}")
    return payment


@app.post("/api/payment/submit-tx")
async def api_submit_tx(req: PaymentSubmitRequest, user=Depends(get_current_user)):
    """Submit a transaction hash for payment verification."""
    payment_id = req.payment_id
    tx_hash = req.tx_hash.strip()

    # The admin will need to verify the tx manually.
    if not submit_payment_tx(payment_id, user["sub"], tx_hash):
        raise HTTPException(404, "Payment not found")

    logger.info(f"[Payment] TX submitted by {user['username']}: {tx_hash}")
    return {"status": "submitted", "message": "Transaction submitted for admin review"}


@app.get("/api/my-payments")
async def api_my_payments(user=Depends(get_current_user)):
    return get_user_payments(user["sub"])


@app.post("/api/redeem-code")
async def api_redeem_code(req: RedeemCodeRequest, user=Depends(get_current_user)):
    try:
        result = redeem_code_for_user(req.code, user["sub"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info(f"[Redeem] User {user['username']} redeemed code {req.code.strip().upper()}")
    return {"status": "redeemed", **result}


# ═══════════════════════════════════════════════
# ADMIN API
# ═══════════════════════════════════════════════

class AdminUserUpdateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    role: str = "user"
    is_active: bool = True
    balance_usdt: float = Field(default=0.0, ge=0.0)


class AdminSubscriptionRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=80)
    duration_days: int = Field(default=0, ge=0, le=3650)
    status: str = "active"


class RegistrationSettingsRequest(BaseModel):
    invite_required: bool = False


class InviteCodeRequest(BaseModel):
    note: str = Field(default="", max_length=200)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: str = Field(default="", max_length=40)


class RedeemCodeCreateRequest(BaseModel):
    plan_id: str = Field(default="", max_length=80)
    duration_days: int = Field(default=0, ge=0, le=3650)
    balance_usdt: float = Field(default=0.0, ge=0.0)
    note: str = Field(default="", max_length=200)
    expires_at: str = Field(default="", max_length=40)


@app.get("/api/admin/users")
async def api_admin_users(admin=Depends(require_admin)):
    users = get_all_users()
    # Add subscription info for each user
    for u in users:
        sub = get_user_active_subscription(u["id"])
        u["subscription"] = sub
    return users


@app.post("/api/admin/user/{user_id}/toggle")
async def api_admin_toggle_user(user_id: str, admin=Depends(require_admin)):
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user_id == admin["sub"]:
        raise HTTPException(400, "You cannot disable your own admin account")
    if user.get("role") == "admin":
        raise HTTPException(400, "Admin accounts cannot be toggled from this endpoint")
    new_status = not bool(user.get("is_active", 1))
    update_user_status(user_id, new_status)
    return {"status": "ok", "is_active": new_status}


@app.put("/api/admin/user/{user_id}")
async def api_admin_update_user(user_id: str, req: AdminUserUpdateRequest, admin=Depends(require_admin)):
    existing = get_user_by_id(user_id)
    if not existing:
        raise HTTPException(404, "User not found")
    if user_id == admin["sub"] and (req.role != "admin" or not req.is_active):
        raise HTTPException(400, "You cannot demote or disable your own admin account")
    try:
        updated = update_user_admin(
            user_id=user_id,
            username=req.username,
            email=req.email,
            role=req.role,
            is_active=req.is_active,
            balance_usdt=req.balance_usdt,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "updated", "user": updated}


@app.post("/api/admin/user/{user_id}/subscription")
async def api_admin_set_user_subscription(user_id: str, req: AdminSubscriptionRequest, admin=Depends(require_admin)):
    if not get_user_by_id(user_id):
        raise HTTPException(404, "User not found")
    if req.status not in ("active", "pending"):
        raise HTTPException(400, "status must be active or pending")
    try:
        sub = set_user_subscription(user_id, req.plan_id, req.status, req.duration_days or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved", "subscription": sub}


@app.get("/api/admin/payments")
async def api_admin_payments(status: str = None, admin=Depends(require_admin)):
    return get_all_payments(status)


@app.post("/api/admin/payment/{payment_id}/confirm")
async def api_admin_confirm_payment(payment_id: str, admin=Depends(require_admin)):
    try:
        confirm_payment(payment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    logger.info(f"[Admin] Payment {payment_id} confirmed")
    return {"status": "confirmed"}


@app.post("/api/admin/payment/{payment_id}/reject")
async def api_admin_reject_payment(payment_id: str, admin=Depends(require_admin)):
    from database import get_connection
    conn = get_connection()
    try:
        conn.execute("UPDATE payments SET status='rejected' WHERE id=?", (payment_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "rejected"}


# Admin subscription plan management
class PlanRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    price_usdt: float = Field(ge=0)
    duration_days: int = Field(default=30, ge=1, le=3650)
    features: list[str] = Field(default_factory=list)
    max_signals_per_day: int = Field(default=0, ge=0)


@app.post("/api/admin/plans")
async def api_admin_create_plan(req: PlanRequest, admin=Depends(require_admin)):
    plan = create_subscription_plan(req.name, req.description, req.price_usdt, req.duration_days, req.features, req.max_signals_per_day)
    return plan


@app.get("/api/admin/plans")
async def api_admin_get_plans(admin=Depends(require_admin)):
    return get_subscription_plans(active_only=False)


@app.put("/api/admin/plans/{plan_id}")
async def api_admin_update_plan(plan_id: str, req: PlanRequest, admin=Depends(require_admin)):
    update_subscription_plan(plan_id, name=req.name, description=req.description, price_usdt=req.price_usdt,
                             duration_days=req.duration_days, features=req.features, max_signals_per_day=req.max_signals_per_day)
    return {"status": "updated"}


@app.delete("/api/admin/plans/{plan_id}")
async def api_admin_delete_plan(plan_id: str, admin=Depends(require_admin)):
    delete_subscription_plan(plan_id)
    return {"status": "deleted"}


# Admin payment address management
class PaymentAddressRequest(BaseModel):
    network: str = Field(min_length=2, max_length=20)
    address: str = Field(min_length=8, max_length=200)


@app.get("/api/admin/payment-addresses")
async def api_admin_get_addresses(admin=Depends(require_admin)):
    return get_all_payment_addresses()


@app.post("/api/admin/payment-addresses")
async def api_admin_set_address(req: PaymentAddressRequest, admin=Depends(require_admin)):
    try:
        set_payment_address(req.network, req.address)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved", "network": req.network.upper().strip()}


@app.get("/api/admin/registration")
async def api_admin_registration(admin=Depends(require_admin)):
    return await api_registration_settings()


@app.post("/api/admin/registration")
async def api_admin_save_registration(req: RegistrationSettingsRequest, admin=Depends(require_admin)):
    set_admin_setting("registration_invite_required", "true" if req.invite_required else "false")
    return {"status": "saved", "invite_required": req.invite_required}


@app.get("/api/admin/invite-codes")
async def api_admin_invite_codes(admin=Depends(require_admin)):
    return list_invite_codes()


@app.post("/api/admin/invite-codes")
async def api_admin_create_invite_code(req: InviteCodeRequest, admin=Depends(require_admin)):
    return create_invite_code(req.note, req.max_uses, req.expires_at, admin["sub"])


@app.get("/api/admin/redeem-codes")
async def api_admin_redeem_codes(admin=Depends(require_admin)):
    return list_redeem_codes()


@app.post("/api/admin/redeem-codes")
async def api_admin_create_redeem_code(req: RedeemCodeCreateRequest, admin=Depends(require_admin)):
    if not req.plan_id and req.balance_usdt <= 0:
        raise HTTPException(400, "Choose a subscription plan or set balance_usdt")
    try:
        return create_redeem_code(
            plan_id=req.plan_id,
            duration_days=req.duration_days,
            balance_usdt=req.balance_usdt,
            note=req.note,
            expires_at=req.expires_at,
            created_by=admin["sub"],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


# ═══════════════════════════════════════════════
# HEALTH & STATUS
# ═══════════════════════════════════════════════

@app.get("/api/status")
async def api_status():
    return {
        "name": "TradingView Signal Server",
        "status": "running",
        "version": "4.0.0",
        "ai_provider": settings.ai.provider,
        "exchange": settings.exchange.name,
        "live_trading": settings.exchange.live_trading,
        "supported_exchanges": get_supported_exchanges(),
        "tp_levels": settings.take_profit.num_levels,
        "trailing_stop_mode": settings.trailing_stop.mode,
        "custom_provider_enabled": settings.ai.custom_provider_enabled,
        "custom_provider_name": settings.ai.custom_provider_name,
        "custom_provider_model": settings.ai.custom_provider_model,
        "custom_provider_url": settings.ai.custom_provider_api_url,
        "risk": {
            "max_position_pct": settings.risk.max_position_pct,
            "max_daily_trades": settings.risk.max_daily_trades,
            "max_daily_loss_pct": settings.risk.max_daily_loss_pct,
            "exit_management_mode": settings.risk.exit_management_mode,
            "custom_stop_loss_pct": settings.risk.custom_stop_loss_pct,
            "ai_exit_system_prompt": settings.risk.ai_exit_system_prompt,
        },
        "time": datetime.utcnow().isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════
# MAIN WEBHOOK ENDPOINT
# ═══════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    """
    Main webhook endpoint for TradingView alerts.
    Pipeline: Parse → Market Data → Pre-Filter → AI Analysis → Decision → Execute → Log
    """
    try:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        try:
            signal = TradingViewSignal(**body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=json.loads(e.json()))

        # Authenticate
        if settings.server.webhook_secret:
            if not hmac.compare_digest(signal.secret, settings.server.webhook_secret):
                logger.warning(f"[Webhook] ❌ Invalid secret from {request.client.host}")
                raise HTTPException(status_code=403, detail="Invalid webhook secret")
        elif settings.exchange.live_trading:
            raise HTTPException(status_code=503, detail="WEBHOOK_SECRET is required for live trading")

        logger.info(f"[Webhook] 📡 Signal: {signal.ticker} {signal.direction.value} @ {signal.price}")
        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        # Fetch market context
        market = await fetch_market_context(signal.ticker)

        # Pre-filter
        filter_result = run_pre_filter(
            signal, market,
            max_daily_trades=settings.risk.max_daily_trades,
            max_daily_loss_pct=settings.risk.max_daily_loss_pct,
        )

        if not filter_result.passed:
            await notify_pre_filter_blocked(signal.ticker, signal.direction.value, filter_result.reason)
            decision = TradeDecision(
                execute=False, ticker=signal.ticker,
                reason=f"Pre-filter: {filter_result.reason}", signal=signal,
            )
            trade_id = log_trade(decision, {"status": "blocked_by_prefilter"})
            return JSONResponse(content={
                "status": "blocked", "trade_id": trade_id,
                "reason": filter_result.reason, "checks": filter_result.checks,
            })

        # AI Analysis
        analysis = await analyze_signal(signal, market)
        await notify_ai_analysis(signal.ticker, analysis)

        # Decision
        decision = _make_decision(signal, analysis, market)

        # Execute
        order_result = {"status": "not_executed"}
        if decision.execute:
            order_result = await execute_trade(decision)
            if order_result.get("status") in ("filled", "simulated", "closed"):
                increment_trade_count()
            await notify_trade_executed(decision, order_result)

        trade_id = log_trade(decision, order_result)
        invalidate_performance_cache()

        return JSONResponse(content={
            "status": "executed" if decision.execute else "rejected",
            "trade_id": trade_id,
            "ai_confidence": analysis.confidence,
            "ai_recommendation": analysis.recommendation,
            "ai_reasoning": analysis.reasoning,
            "tp_levels": len(decision.take_profit_levels),
            "trailing_stop": decision.trailing_stop.mode.value if decision.trailing_stop else "none",
            "order": order_result,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[Webhook] Pipeline error: {e}")
        await notify_error(f"Pipeline error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Decision logic
# ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.5
RISK_THRESHOLD = 0.8


def _make_decision(signal, analysis, market) -> TradeDecision:
    recommendation = (analysis.recommendation or "hold").lower().strip()
    if recommendation == "reject":
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"AI rejected (conf={analysis.confidence:.2f}): {analysis.reasoning}",
            signal=signal, ai_analysis=analysis,
        )
    if recommendation not in {"execute", "modify"}:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"AI recommendation is {recommendation}; not executing",
            signal=signal, ai_analysis=analysis,
        )
    if analysis.confidence < CONFIDENCE_THRESHOLD:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"Confidence too low: {analysis.confidence:.2f}",
            signal=signal, ai_analysis=analysis,
        )
    if analysis.risk_score > RISK_THRESHOLD:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason=f"Risk too high: {analysis.risk_score:.2f}",
            signal=signal, ai_analysis=analysis,
        )

    direction = analysis.suggested_direction or signal.direction
    entry = analysis.suggested_entry or signal.price or market.current_price
    if direction in (SignalDirection.CLOSE_LONG, SignalDirection.CLOSE_SHORT):
        return TradeDecision(
            execute=True, direction=direction, ticker=signal.ticker,
            entry_price=entry, reason=f"AI approved close: {analysis.reasoning}",
            signal=signal, ai_analysis=analysis,
        )
    if not entry or entry <= 0:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason="Cannot calculate entry price",
            signal=signal, ai_analysis=analysis,
        )

    stop_loss = _build_stop_loss(analysis, entry, direction)
    size_multiplier = _clamp(analysis.position_size_pct, 0.0, 1.0)
    qty = _calc_qty(entry, stop_loss, market) * size_multiplier
    if qty <= 0:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason="Calculated quantity is zero",
            signal=signal, ai_analysis=analysis,
        )

    tp_levels = _build_tp_levels(analysis, entry, direction)
    trailing_config = _build_trailing_config()

    return TradeDecision(
        execute=True, direction=direction, ticker=signal.ticker,
        entry_price=entry, stop_loss=stop_loss,
        take_profit=analysis.suggested_take_profit,
        take_profit_levels=tp_levels,
        trailing_stop=trailing_config,
        quantity=round(qty, 6),
        reason=f"AI approved (conf={analysis.confidence:.2f}): {analysis.reasoning}",
        signal=signal, ai_analysis=analysis,
    )


def _build_tp_levels(analysis, entry, direction) -> list[TakeProfitLevel]:
    tp_levels = []
    num = int(_clamp(settings.take_profit.num_levels, 1, 4))
    is_long = direction in (SignalDirection.LONG,)

    for i in range(1, num + 1):
        ai_tp = getattr(analysis, f"suggested_tp{i}", None)
        ai_qty = getattr(analysis, f"tp{i}_qty_pct", 25.0)
        default_pct = getattr(settings.take_profit, f"tp{i}_pct", 2.0 * i)
        default_qty = getattr(settings.take_profit, f"tp{i}_qty", 25.0)

        use_ai_tp = settings.risk.exit_management_mode == "ai"
        if use_ai_tp and ai_tp and ai_tp > 0 and ((is_long and ai_tp > entry) or (not is_long and ai_tp < entry)):
            price = ai_tp
        else:
            pct = default_pct / 100.0
            price = entry * (1 + pct) if is_long else entry * (1 - pct)

        qty_pct = ai_qty if ai_qty != 25.0 else default_qty
        qty_pct = _clamp(qty_pct, 1.0, 100.0)
        tp_levels.append(TakeProfitLevel(price=round(price, 8), qty_pct=qty_pct))

    total_qty = sum(tp.qty_pct for tp in tp_levels)
    if total_qty > 100:
        scale = 100 / total_qty
        tp_levels = [
            TakeProfitLevel(price=tp.price, qty_pct=round(tp.qty_pct * scale, 4))
            for tp in tp_levels
        ]

    return tp_levels


def _build_stop_loss(analysis, entry, direction) -> float | None:
    is_long = direction in (SignalDirection.LONG,)
    if settings.risk.exit_management_mode == "ai":
        sl = analysis.suggested_stop_loss
        if sl and sl > 0 and ((is_long and sl < entry) or (not is_long and sl > entry)):
            return sl

    pct = settings.risk.custom_stop_loss_pct / 100.0
    return round(entry * (1 - pct if is_long else 1 + pct), 8)


def _build_trailing_config() -> TrailingStopConfig:
    mode_str = settings.trailing_stop.mode.lower()
    try:
        mode = TrailingStopMode(mode_str)
    except ValueError:
        mode = TrailingStopMode.NONE

    return TrailingStopConfig(
        mode=mode,
        trail_pct=_clamp(settings.trailing_stop.trail_pct, 0.1, 20.0),
        activation_profit_pct=_clamp(settings.trailing_stop.activation_profit_pct, 0.1, 50.0),
        trailing_step_pct=_clamp(settings.trailing_stop.trailing_step_pct, 0.1, 10.0),
    )


def _calc_qty(entry, stop_loss, market, risk_pct=1.0):
    if not entry or entry <= 0:
        return 0.0
    account_equity = max(settings.risk.account_equity_usdt, 0)
    if account_equity <= 0:
        return 0.0
    if stop_loss and stop_loss > 0:
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit > 0:
            risk_capital = account_equity * risk_pct * 0.01
            max_qty = (account_equity * settings.risk.max_position_pct * 0.01) / entry
            return min(risk_capital / risk_per_unit, max_qty)
    return (account_equity * settings.risk.max_position_pct * 0.01) / entry


def _clamp(value, low, high):
    return max(low, min(high, float(value)))


# ═══════════════════════════════════════════════
# DASHBOARD API ENDPOINTS (require auth)
# ═══════════════════════════════════════════════

@app.get("/stats")
async def stats(user=Depends(require_admin)):
    return get_today_stats()


@app.get("/trades")
async def trades(user=Depends(require_admin)):
    return get_today_trades()


@app.get("/balance")
async def balance(user=Depends(require_admin)):
    return await get_account_balance()


@app.get("/api/positions")
async def api_positions(user=Depends(require_admin)):
    return await get_open_positions()


@app.get("/api/orders")
async def api_orders(symbol: str = None, limit: int = 50, user=Depends(require_admin)):
    limit = max(1, min(limit, 200))
    return await get_recent_orders(symbol, limit)


@app.get("/api/history")
async def api_history(days: int = 30, user=Depends(require_admin)):
    days = max(1, min(days, 365))
    return get_trade_history(days)


@app.get("/api/performance")
async def api_performance(days: int = 30, user=Depends(require_admin)):
    days = max(1, min(days, 365))
    return calculate_performance(days)


@app.get("/api/daily-pnl")
async def api_daily_pnl(days: int = 30, user=Depends(require_admin)):
    days = max(1, min(days, 365))
    return get_daily_pnl(days)


@app.get("/api/distribution")
async def api_distribution(user=Depends(require_admin)):
    return get_trade_distribution()


# ── Connection Test ──
class ConnectionTestRequest(BaseModel):
    exchange: str = Field(min_length=2, max_length=30)
    api_key: str = Field(min_length=1, max_length=300)
    api_secret: str = Field(min_length=1, max_length=300)
    password: str = ""


@app.post("/api/test-connection")
async def api_test_connection(req: ConnectionTestRequest, user=Depends(require_admin)):
    return await test_exchange_connection(req.exchange, req.api_key, req.api_secret, req.password)


# ── Settings (admin only) ──
class ExchangeSettingsRequest(BaseModel):
    exchange: str = ""
    api_key: str = Field(default="", max_length=300)
    api_secret: str = Field(default="", max_length=300)
    password: str = Field(default="", max_length=300)


class AISettingsRequest(BaseModel):
    provider: str = ""
    api_key: str = Field(default="", max_length=500)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, ge=128, le=8000)
    custom_system_prompt: str = ""
    custom_provider_enabled: bool = False
    custom_provider_name: str = "custom"
    custom_provider_model: str = ""
    custom_provider_api_url: str = ""


class TelegramSettingsRequest(BaseModel):
    bot_token: str = Field(default="", max_length=300)
    chat_id: str = Field(default="", max_length=100)


class RiskSettingsRequest(BaseModel):
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100.0)
    max_daily_trades: int = Field(default=10, ge=0, le=1000)
    max_daily_loss_pct: float = Field(default=5.0, ge=0.1, le=100.0)
    exit_management_mode: str = "ai"
    custom_stop_loss_pct: float = Field(default=1.5, ge=0.1, le=100.0)
    ai_exit_system_prompt: str = Field(default="", max_length=4000)


class TakeProfitSettingsRequest(BaseModel):
    num_levels: int = Field(default=1, ge=1, le=4)
    tp1_pct: float = Field(default=2.0, gt=0, le=200.0)
    tp2_pct: float = Field(default=4.0, gt=0, le=200.0)
    tp3_pct: float = Field(default=6.0, gt=0, le=200.0)
    tp4_pct: float = Field(default=10.0, gt=0, le=200.0)
    tp1_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp2_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp3_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp4_qty: float = Field(default=25.0, ge=0.0, le=100.0)


class TrailingStopSettingsRequest(BaseModel):
    mode: str = "none"
    trail_pct: float = Field(default=1.0, ge=0.1, le=20.0)
    activation_profit_pct: float = Field(default=1.0, ge=0.1, le=50.0)
    trailing_step_pct: float = Field(default=0.5, ge=0.1, le=10.0)


@app.post("/api/settings/exchange")
async def save_exchange_settings(req: ExchangeSettingsRequest, admin=Depends(require_admin)):
    if req.exchange:
        exchange_name = req.exchange.lower().strip()
        if exchange_name not in get_supported_exchanges():
            raise HTTPException(400, f"Unsupported exchange: {req.exchange}")
        settings.exchange.name = exchange_name
    if req.api_key:
        settings.exchange.api_key = req.api_key
    if req.api_secret:
        settings.exchange.api_secret = req.api_secret
    if req.password:
        settings.exchange.password = req.password
    _save_runtime_settings({"exchange": {"name": settings.exchange.name}})
    logger.info(f"[Settings] Exchange updated: {settings.exchange.name}")
    return {"status": "saved", "exchange": settings.exchange.name}


@app.post("/api/settings/ai")
async def save_ai_settings(req: AISettingsRequest, admin=Depends(require_admin)):
    valid_providers = {"openai", "anthropic", "deepseek", "custom", req.custom_provider_name}
    if req.provider:
        provider = req.provider.lower().strip()
        if provider not in valid_providers:
            raise HTTPException(400, f"Unsupported AI provider: {req.provider}")
        settings.ai.provider = provider
    if req.api_key:
        active_provider = settings.ai.provider
        if active_provider == "openai":
            settings.ai.openai_api_key = req.api_key
        elif active_provider == "anthropic":
            settings.ai.anthropic_api_key = req.api_key
        elif active_provider == "deepseek":
            settings.ai.deepseek_api_key = req.api_key
        elif active_provider in {"custom", req.custom_provider_name}:
            settings.ai.custom_provider_api_key = req.api_key

    settings.ai.custom_provider_enabled = req.custom_provider_enabled
    if req.custom_provider_name:
        settings.ai.custom_provider_name = req.custom_provider_name
    if req.custom_provider_model:
        settings.ai.custom_provider_model = req.custom_provider_model
    if req.custom_provider_api_url:
        settings.ai.custom_provider_api_url = req.custom_provider_api_url
    settings.ai.temperature = req.temperature
    settings.ai.max_tokens = req.max_tokens
    settings.ai.custom_system_prompt = req.custom_system_prompt

    _save_runtime_settings({
        "ai": {
            "provider": settings.ai.provider,
            "temperature": settings.ai.temperature,
            "max_tokens": settings.ai.max_tokens,
            "custom_system_prompt": settings.ai.custom_system_prompt,
            "custom_provider_enabled": settings.ai.custom_provider_enabled,
            "custom_provider_name": settings.ai.custom_provider_name,
            "custom_provider_model": settings.ai.custom_provider_model,
            "custom_provider_api_url": settings.ai.custom_provider_api_url,
        }
    })
    logger.info(f"[Settings] AI provider updated: {settings.ai.provider}")
    return {"status": "saved", "provider": settings.ai.provider}


@app.post("/api/settings/telegram")
async def save_telegram_settings(req: TelegramSettingsRequest, admin=Depends(require_admin)):
    if req.bot_token:
        settings.telegram.bot_token = req.bot_token
    if req.chat_id:
        settings.telegram.chat_id = req.chat_id
    _save_runtime_settings({"telegram": {"chat_id": settings.telegram.chat_id}})
    logger.info("[Settings] Telegram updated")
    return {"status": "saved"}


@app.post("/api/settings/risk")
async def save_risk_settings(req: RiskSettingsRequest, admin=Depends(require_admin)):
    if req.exit_management_mode not in ("ai", "custom"):
        raise HTTPException(400, "exit_management_mode must be ai or custom")
    settings.risk.max_position_pct = req.max_position_pct
    settings.risk.max_daily_trades = req.max_daily_trades
    settings.risk.max_daily_loss_pct = req.max_daily_loss_pct
    settings.risk.exit_management_mode = req.exit_management_mode
    settings.risk.custom_stop_loss_pct = req.custom_stop_loss_pct
    settings.risk.ai_exit_system_prompt = req.ai_exit_system_prompt
    _save_runtime_settings({
        "risk": {
            "max_position_pct": req.max_position_pct,
            "max_daily_trades": req.max_daily_trades,
            "max_daily_loss_pct": req.max_daily_loss_pct,
            "exit_management_mode": req.exit_management_mode,
            "custom_stop_loss_pct": req.custom_stop_loss_pct,
            "ai_exit_system_prompt": req.ai_exit_system_prompt,
        }
    })
    logger.info("[Settings] Risk settings updated")
    return {"status": "saved"}


@app.post("/api/settings/take-profit")
async def save_take_profit_settings(req: TakeProfitSettingsRequest, admin=Depends(require_admin)):
    active_qty = [req.tp1_qty, req.tp2_qty, req.tp3_qty, req.tp4_qty][:req.num_levels]
    if any(q <= 0 for q in active_qty):
        raise HTTPException(400, "Active TP close percentages must be greater than 0")
    total_qty = sum(active_qty)
    if total_qty > 100:
        raise HTTPException(400, f"Total active TP close percentage is {total_qty:.2f}%, must be <= 100%")
    settings.take_profit.num_levels = req.num_levels
    settings.take_profit.tp1_pct = req.tp1_pct
    settings.take_profit.tp2_pct = req.tp2_pct
    settings.take_profit.tp3_pct = req.tp3_pct
    settings.take_profit.tp4_pct = req.tp4_pct
    settings.take_profit.tp1_qty = req.tp1_qty
    settings.take_profit.tp2_qty = req.tp2_qty
    settings.take_profit.tp3_qty = req.tp3_qty
    settings.take_profit.tp4_qty = req.tp4_qty
    _save_runtime_settings({
        "take_profit": {
            "num_levels": req.num_levels,
            "tp1_pct": req.tp1_pct, "tp2_pct": req.tp2_pct,
            "tp3_pct": req.tp3_pct, "tp4_pct": req.tp4_pct,
            "tp1_qty": req.tp1_qty, "tp2_qty": req.tp2_qty,
            "tp3_qty": req.tp3_qty, "tp4_qty": req.tp4_qty,
        }
    })
    logger.info(f"[Settings] Take-profit updated: {req.num_levels} levels")
    return {"status": "saved", "num_levels": req.num_levels}


@app.post("/api/settings/trailing-stop")
async def save_trailing_stop_settings(req: TrailingStopSettingsRequest, admin=Depends(require_admin)):
    try:
        TrailingStopMode(req.mode)
    except ValueError:
        raise HTTPException(400, f"Unsupported trailing-stop mode: {req.mode}")
    settings.trailing_stop.mode = req.mode
    settings.trailing_stop.trail_pct = req.trail_pct
    settings.trailing_stop.activation_profit_pct = req.activation_profit_pct
    settings.trailing_stop.trailing_step_pct = req.trailing_step_pct
    _save_runtime_settings({
        "trailing_stop": {
            "mode": req.mode, "trail_pct": req.trail_pct,
            "activation_profit_pct": req.activation_profit_pct,
            "trailing_step_pct": req.trailing_step_pct,
        }
    })
    logger.info(f"[Settings] Trailing stop updated: {req.mode}")
    return {"status": "saved", "mode": req.mode}


@app.post("/api/test-telegram")
async def api_test_telegram(admin=Depends(require_admin)):
    await send_telegram("🧪 <b>Test Message</b>\n\nTradingView Signal Server is connected!")
    return {"status": "sent"}


@app.post("/test-signal")
async def test_signal(admin=Depends(require_admin)):
    market = await fetch_market_context("BTCUSDT")
    signal = TradingViewSignal(
        secret=settings.server.webhook_secret,
        ticker="BTCUSDT", exchange="BINANCE",
        direction=SignalDirection.LONG,
        price=market.current_price,
        timeframe="60", strategy="Test Signal",
        message="Manual test",
    )
    return await _process_internal(signal)


async def _process_internal(signal):
    market = await fetch_market_context(signal.ticker)
    fr = run_pre_filter(signal, market,
        max_daily_trades=settings.risk.max_daily_trades,
        max_daily_loss_pct=settings.risk.max_daily_loss_pct)
    if not fr.passed:
        return {"status": "blocked", "reason": fr.reason}

    analysis = await analyze_signal(signal, market)
    decision = _make_decision(signal, analysis, market)
    order_result = {"status": "not_executed"}
    if decision.execute:
        order_result = await execute_trade(decision)
        if order_result.get("status") in ("filled", "simulated", "closed"):
            increment_trade_count()
    trade_id = log_trade(decision, order_result)
    invalidate_performance_cache()
    return {
        "status": "executed" if decision.execute else "rejected",
        "trade_id": trade_id,
        "ai": {"confidence": analysis.confidence, "recommendation": analysis.recommendation, "reasoning": analysis.reasoning},
        "tp_levels": len(decision.take_profit_levels),
        "trailing_stop": decision.trailing_stop.mode.value if decision.trailing_stop else "none",
        "order": order_result,
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.server.host, port=settings.server.port, reload=True)

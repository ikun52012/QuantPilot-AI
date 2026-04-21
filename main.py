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
import os
import sys
import json
import hmac
import hashlib
import secrets
import threading
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from typing import Optional

from config import settings
from security import decrypt_settings_payload, encrypt_settings_payload
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
    AUTH_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    hash_password,
    verify_password,
    validate_password_strength,
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
    create_user_admin,
    delete_user_admin,
    update_user_admin,
    update_user_status,
    update_user_password_hash,
    get_user_settings,
    update_user_settings,
    ensure_user_webhook_secret,
    find_user_by_webhook_secret,
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
    get_payment_by_id,
    payment_tx_hash_exists,
    get_user_payments,
    get_all_payments,
    get_admin_setting,
    set_admin_setting,
    add_admin_audit_log,
    get_admin_audit_logs,
    has_recent_webhook_event,
    acquire_webhook_fingerprint,
    mark_webhook_fingerprint,
    record_webhook_event,
    get_webhook_events,
    get_operational_counts,
    create_invite_code,
    list_invite_codes,
    is_invite_code_valid,
    validate_and_consume_invite,
    create_redeem_code,
    list_redeem_codes,
    redeem_code_for_user,
    get_connection,
)
from backups import create_backup, list_backups, backup_path, stage_restore
from chain_verify import verify_payment_tx
from position_monitor import get_monitor_state, run_position_monitor_once
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
_SENSITIVE_LOG_RE = re.compile(r"(?i)(api[_-]?key|api[_-]?secret|secret|password|token)(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+")


def _sanitize_log_record(record):
    record["message"] = _SENSITIVE_LOG_RE.sub(r"\1\2***", record["message"])
    return True


logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}", filter=_sanitize_log_record)
logger.add(
    "logs/server_{time:YYYY-MM-DD}.log",
    rotation="100 MB",
    retention="30 days",
    level="DEBUG",
    filter=_sanitize_log_record,
)
if os.getenv("JSON_LOGS", "false").lower() == "true":
    logger.add("logs/server.jsonl", rotation="100 MB", retention="30 days", level="INFO", serialize=True, filter=_sanitize_log_record)

# ─────────────────────────────────────────────
# Simple in-memory login rate limiter
# ─────────────────────────────────────────────
_login_attempts: dict[str, list] = {}  # ip -> [timestamp, ...]
_login_rate_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 10   # max attempts per window
_LOGIN_WINDOW_SECS = 300   # 5-minute sliding window
_register_attempts: dict[str, list] = {}
_register_rate_lock = threading.Lock()
_REGISTER_MAX_ATTEMPTS = 5
_REGISTER_WINDOW_SECS = 600


def _check_login_rate_limit(ip: str) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    now = datetime.utcnow().timestamp()
    cutoff = now - _LOGIN_WINDOW_SECS
    with _login_rate_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if t > cutoff]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            _login_attempts[ip] = attempts
            return False
        attempts.append(now)
        _login_attempts[ip] = attempts
    return True


def _clear_login_rate_limit(ip: str):
    """Clear login attempt counters after a successful login."""
    with _login_rate_lock:
        _login_attempts.pop(ip, None)


def _check_register_rate_limit(ip: str) -> bool:
    now = datetime.utcnow().timestamp()
    cutoff = now - _REGISTER_WINDOW_SECS
    with _register_rate_lock:
        attempts = [t for t in _register_attempts.get(ip, []) if t > cutoff]
        if len(attempts) >= _REGISTER_MAX_ATTEMPTS:
            _register_attempts[ip] = attempts
            return False
        attempts.append(now)
        _register_attempts[ip] = attempts
        return True

# Settings file for runtime config changes.
# Store it under data/ because docker-compose already persists /app/data.
DATA_DIR = Path(__file__).parent / "data"
SETTINGS_FILE = DATA_DIR / "runtime_settings.json"
LEGACY_SETTINGS_FILE = Path(__file__).parent / "runtime_settings.json"
_settings_lock = threading.Lock()
PLACEHOLDER_WEBHOOK_SECRETS = {
    "",
    "replace-with-a-long-random-webhook-secret",
    "your-webhook-secret",
    "your_webhook_secret",
    "changeme",
    "change-me",
}


def _is_placeholder_webhook_secret(value: str | None) -> bool:
    normalized = (value or "").strip()
    return normalized.lower() in PLACEHOLDER_WEBHOOK_SECRETS


def _is_placeholder_public_base_url(value: str | None) -> bool:
    normalized = (value or "").strip().lower().rstrip("/")
    return normalized in {
        "",
        "https://your-domain.example",
        "http://your-domain.example",
        "https://example.com",
        "http://example.com",
        "https://<your-domain>",
        "http://<your-domain>",
    } or "your-domain" in normalized


def _load_runtime_settings() -> dict:
    for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        if not path.exists():
            continue
        try:
            return decrypt_settings_payload(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"[Settings] Failed to read {path.name}: {e}")
    return {}


def _save_runtime_settings(data: dict):
    with _settings_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        current = _load_runtime_settings()
        current.update(data)
        tmp_path = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(encrypt_settings_payload(current), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(SETTINGS_FILE)


def _public_base_url(request: Request | None = None) -> str:
    if not _is_placeholder_public_base_url(settings.server.public_base_url):
        return settings.server.public_base_url.rstrip("/")
    if request:
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        if forwarded_host:
            proto = forwarded_proto or request.url.scheme
            return f"{proto}://{forwarded_host}".rstrip("/")
        return str(request.base_url).rstrip("/")
    return ""


def _client_ip(request: Request | None) -> str:
    if not request:
        return ""
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _audit(admin: dict, action: str, target_type: str = "", target_id: str = "", summary: str = "", request: Request | None = None):
    try:
        add_admin_audit_log(
            admin_id=admin.get("sub", ""),
            admin_username=admin.get("username", ""),
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
            client_ip=_client_ip(request),
        )
    except Exception as e:
        logger.debug(f"[Audit] Could not write audit log: {e}")


def _webhook_fingerprint(body: dict, user_id: str | None) -> str:
    scope = user_id or "admin"
    alert_id = str(body.get("alert_id") or body.get("order_id") or body.get("id") or "").strip()
    fields = {
        "scope": scope,
        "secret_hash": hashlib.sha256(str(body.get("secret", "")).strip().encode()).hexdigest()[:16],
        "ticker": str(body.get("ticker", "")).upper().strip(),
        "direction": str(body.get("direction", "")).lower().strip(),
        "timeframe": str(body.get("timeframe", "")).strip(),
        "price": round(float(body.get("price") or 0), 8),
        "strategy": str(body.get("strategy", "")).strip(),
        "message": str(body.get("message", "")).strip(),
    }
    if alert_id:
        fields = {"scope": scope, "secret_hash": fields["secret_hash"], "alert_id": alert_id}
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _verify_webhook_signature(request: Request, raw_body: bytes) -> bool:
    hmac_secret = os.getenv("WEBHOOK_HMAC_SECRET", "").strip()
    if not hmac_secret:
        return True
    supplied = (
        request.headers.get("x-tvss-signature", "")
        or request.headers.get("x-signal-signature", "")
        or request.headers.get("x-webhook-signature", "")
    ).strip()
    if not supplied:
        return False
    digest = hmac.new(hmac_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(supplied, expected) or hmac.compare_digest(supplied, digest)


def _disabled_pre_filter_checks() -> set[str]:
    raw = get_admin_setting("pre_filter_disabled_checks", os.getenv("PREFILTER_DISABLED_CHECKS", ""))
    return {item.strip() for item in raw.split(",") if item.strip()}


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
        settings.exchange.api_key = rs["exchange"].get("api_key", settings.exchange.api_key)
        settings.exchange.api_secret = rs["exchange"].get("api_secret", settings.exchange.api_secret)
        settings.exchange.password = rs["exchange"].get("password", settings.exchange.password)
    if rs.get("ai"):
        settings.ai.provider = rs["ai"].get("provider", settings.ai.provider)
        settings.ai.openai_api_key = rs["ai"].get("openai_api_key", settings.ai.openai_api_key)
        settings.ai.anthropic_api_key = rs["ai"].get("anthropic_api_key", settings.ai.anthropic_api_key)
        settings.ai.deepseek_api_key = rs["ai"].get("deepseek_api_key", settings.ai.deepseek_api_key)
        settings.ai.custom_provider_api_key = rs["ai"].get("custom_provider_api_key", settings.ai.custom_provider_api_key)
        settings.ai.temperature = rs["ai"].get("temperature", settings.ai.temperature)
        settings.ai.max_tokens = rs["ai"].get("max_tokens", settings.ai.max_tokens)
        settings.ai.custom_system_prompt = rs["ai"].get("custom_system_prompt", settings.ai.custom_system_prompt)
        settings.ai.custom_provider_enabled = rs["ai"].get("custom_provider_enabled", settings.ai.custom_provider_enabled)
        settings.ai.custom_provider_name = rs["ai"].get("custom_provider_name", settings.ai.custom_provider_name)
        settings.ai.custom_provider_model = rs["ai"].get("custom_provider_model", settings.ai.custom_provider_model)
        settings.ai.custom_provider_api_url = rs["ai"].get("custom_provider_api_url", settings.ai.custom_provider_api_url)
    if rs.get("telegram"):
        settings.telegram.bot_token = rs["telegram"].get("bot_token", settings.telegram.bot_token)
        settings.telegram.chat_id = rs["telegram"].get("chat_id", settings.telegram.chat_id)
    if rs.get("risk"):
        settings.risk.account_equity_usdt = rs["risk"].get("account_equity_usdt", settings.risk.account_equity_usdt)
        settings.risk.max_position_pct = rs["risk"].get("max_position_pct", settings.risk.max_position_pct)
        settings.risk.max_daily_trades = rs["risk"].get("max_daily_trades", settings.risk.max_daily_trades)
        settings.risk.max_daily_loss_pct = rs["risk"].get("max_daily_loss_pct", settings.risk.max_daily_loss_pct)
        settings.risk.exit_management_mode = rs["risk"].get("exit_management_mode", settings.risk.exit_management_mode)
        settings.risk.ai_risk_profile = rs["risk"].get("ai_risk_profile", settings.risk.ai_risk_profile)
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
    if rs:
        _save_runtime_settings(rs)

    env_webhook_secret = (settings.server.webhook_secret or "").strip()
    if _is_placeholder_webhook_secret(env_webhook_secret):
        stored_secret = get_admin_setting("webhook_secret", "")
        if _is_placeholder_webhook_secret(stored_secret):
            stored_secret = secrets.token_urlsafe(32)
            set_admin_setting("webhook_secret", stored_secret)
            logger.warning("[Security] Generated a persistent admin webhook secret. Keep it private.")
        settings.server.webhook_secret = stored_secret
    else:
        settings.server.webhook_secret = env_webhook_secret

    # ── APScheduler: daily reset at midnight UTC ──
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from pre_filter import reset_daily_counters

    scheduler = AsyncIOScheduler()

    async def _daily_reset_job():
        """Runs at midnight UTC: reset in-memory daily trade counters."""
        reset_daily_counters()
        logger.info("[Scheduler] Daily trade counters reset")
        # Optional: send Telegram summary
        try:
            from trade_logger import get_today_stats
            stats = get_today_stats()
            msg = (
                f"📊 Daily Summary\n"
                f"Signals: {stats['total_signals']} | Executed: {stats['executed']} | "
                f"Rejected: {stats['rejected']}"
            )
            from notifier import send_telegram
            await send_telegram(msg)
        except Exception as exc:
            logger.debug(f"[Scheduler] Telegram summary skipped: {exc}")

    async def _position_monitor_job():
        try:
            users = get_all_users()
            user_configs = {}
            for u in users:
                cfg = get_user_settings(u["id"])
                if cfg:
                    user_configs[u["id"]] = cfg
            result = await run_position_monitor_once(user_configs)
            if result.get("adjusted"):
                logger.info(f"[PositionMonitor] Adjusted {result['adjusted']} protective stop(s)")
        except Exception as exc:
            logger.debug(f"[PositionMonitor] Scheduled run skipped: {exc}")

    scheduler.add_job(_daily_reset_job, CronTrigger(hour=0, minute=0, second=0, timezone="UTC"))
    scheduler.add_job(_position_monitor_job, "interval", seconds=int(os.getenv("POSITION_MONITOR_INTERVAL_SECS", "60")))
    scheduler.start()
    logger.info("[Scheduler] APScheduler started — daily reset wired at 00:00 UTC")

    yield

    scheduler.shutdown(wait=True)
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


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return await call_next(request)
    if request.url.path in {"/webhook", "/api/auth/login", "/api/auth/register", "/api/auth/logout"}:
        return await call_next(request)
    if request.cookies.get("tvss_token"):
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
        header_token = request.headers.get("x-csrf-token", "")
        if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
            return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})
    return await call_next(request)


# Mount static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie",
}


def _apply_no_store_headers(response: Response) -> Response:
    for key, value in _NO_STORE_HEADERS.items():
        response.headers[key] = value
    return response


def _file_response_no_store(path: Path) -> FileResponse:
    return _apply_no_store_headers(FileResponse(path))


def _redirect_no_store(url: str, request: Request | None = None, clear_cookie: bool = False) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=303)
    if clear_cookie:
        clear_auth_cookie(response, request)
    return _apply_no_store_headers(response)


def _auth_page_response(request: Request, path: Path) -> Response:
    if get_optional_user(request):
        return _redirect_no_store("/dashboard", request)
    response = _file_response_no_store(path)
    if request.cookies.get(AUTH_COOKIE_NAME):
        clear_auth_cookie(response, request)
    return response


# ═══════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    if get_optional_user(request):
        return _redirect_no_store("/dashboard", request)
    return _file_response_no_store(STATIC_DIR / "home.html")

@app.get("/dashboard")
async def dashboard(request: Request):
    if not get_optional_user(request):
        return _redirect_no_store("/login", request, clear_cookie=True)
    return _file_response_no_store(STATIC_DIR / "index.html")

@app.get("/login")
async def login_page(request: Request):
    return _auth_page_response(request, STATIC_DIR / "login.html")

@app.get("/register")
async def register_page(request: Request):
    return _auth_page_response(request, STATIC_DIR / "register.html")


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


class UserExchangeSettingsRequest(BaseModel):
    exchange: str = Field(default="binance", max_length=40)
    api_key: str = Field(default="", max_length=300)
    api_secret: str = Field(default="", max_length=300)
    password: str = Field(default="", max_length=300)
    live_trading: bool = False


class UserTakeProfitSettingsRequest(BaseModel):
    num_levels: int = Field(default=1, ge=1, le=4)
    tp1_pct: float = Field(default=2.0, gt=0, le=200.0)
    tp2_pct: float = Field(default=4.0, gt=0, le=200.0)
    tp3_pct: float = Field(default=6.0, gt=0, le=200.0)
    tp4_pct: float = Field(default=10.0, gt=0, le=200.0)
    tp1_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp2_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp3_qty: float = Field(default=25.0, ge=0.0, le=100.0)
    tp4_qty: float = Field(default=25.0, ge=0.0, le=100.0)


@app.post("/api/auth/register")
async def api_register(req: RegisterRequest, request: Request, response: Response):
    _apply_no_store_headers(response)
    client_ip = _client_ip(request) or "unknown"
    if not _check_register_rate_limit(client_ip):
        logger.warning(f"[Auth] Rate limit hit for registration from {client_ip}")
        raise HTTPException(429, "Too many registration attempts. Please wait 10 minutes.")
    username = req.username.lower().strip()
    email = req.email.lower().strip()
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    ok, reason = validate_password_strength(req.password, username=username, email=email)
    if not ok:
        raise HTTPException(400, reason)
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
    token = create_token(user["id"], user["username"], user["role"], user.get("token_version", 0))
    set_auth_cookie(response, token, request)

    logger.info(f"[Auth] New user registered: {username}")
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "email": email, "role": user["role"]}}


@app.post("/api/auth/login")
async def api_login(req: LoginRequest, request: Request, response: Response):
    _apply_no_store_headers(response)
    client_ip = _client_ip(request) or "unknown"
    if not _check_login_rate_limit(client_ip):
        logger.warning(f"[Auth] Rate limit hit for login from {client_ip}")
        raise HTTPException(429, "Too many login attempts. Please wait 5 minutes.")

    username = req.username.lower().strip()
    user = get_user_by_username(username)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")

    if not user.get("is_active", 1):
        raise HTTPException(403, "Account is disabled")

    update_user_login(user["id"])
    _clear_login_rate_limit(client_ip)
    token = create_token(user["id"], user["username"], user["role"], user.get("token_version", 0))
    set_auth_cookie(response, token, request)

    logger.info(f"[Auth] User logged in: {username}")
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/logout")
async def api_logout(request: Request, response: Response):
    _apply_no_store_headers(response)
    clear_auth_cookie(response, request)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def api_me(response: Response, user=Depends(get_current_user)):
    _apply_no_store_headers(response)
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
    if payment_tx_hash_exists(tx_hash, exclude_payment_id=payment_id):
        raise HTTPException(400, "This transaction hash has already been submitted")

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

def _public_user_settings(user_id: str, request: Request | None = None) -> dict:
    user_settings = get_user_settings(user_id)
    db_user = get_user_by_id(user_id) or {}
    if not (user_settings.get("webhook") or {}).get("secret"):
        ensure_user_webhook_secret(user_id)
        user_settings = get_user_settings(user_id)
    exchange_cfg = user_settings.get("exchange") or {}
    tp_cfg = user_settings.get("take_profit") or {}
    webhook_secret = user_settings.get("webhook", {}).get("secret", "")
    webhook_url = (_public_base_url(request) + "/webhook") if request else "/webhook"
    live_allowed = bool(db_user.get("live_trading_allowed", 0))
    return {
        "exchange": {
            "exchange": exchange_cfg.get("exchange", settings.exchange.name),
            "live_trading": bool(exchange_cfg.get("live_trading", False)) and live_allowed,
            "api_configured": bool(exchange_cfg.get("api_key") and exchange_cfg.get("api_secret")),
        },
        "trade_controls": {
            "live_trading_allowed": live_allowed,
            "max_leverage": int(db_user.get("max_leverage") or 20),
            "max_position_pct": float(db_user.get("max_position_pct") or 10.0),
        },
        "take_profit": {
            "num_levels": tp_cfg.get("num_levels", settings.take_profit.num_levels),
            "tp1_pct": tp_cfg.get("tp1_pct", settings.take_profit.tp1_pct),
            "tp2_pct": tp_cfg.get("tp2_pct", settings.take_profit.tp2_pct),
            "tp3_pct": tp_cfg.get("tp3_pct", settings.take_profit.tp3_pct),
            "tp4_pct": tp_cfg.get("tp4_pct", settings.take_profit.tp4_pct),
            "tp1_qty": tp_cfg.get("tp1_qty", settings.take_profit.tp1_qty),
            "tp2_qty": tp_cfg.get("tp2_qty", settings.take_profit.tp2_qty),
            "tp3_qty": tp_cfg.get("tp3_qty", settings.take_profit.tp3_qty),
            "tp4_qty": tp_cfg.get("tp4_qty", settings.take_profit.tp4_qty),
        },
        "webhook": {
            "url": webhook_url,
            "secret": webhook_secret,
            "template": _tradingview_template(webhook_secret),
        },
    }


@app.get("/api/user/settings")
async def api_user_settings(request: Request, user=Depends(get_current_user)):
    return _public_user_settings(user["sub"], request)


@app.post("/api/user/settings/exchange")
async def api_user_save_exchange(req: UserExchangeSettingsRequest, user=Depends(get_current_user)):
    exchange_name = req.exchange.lower().strip()
    if exchange_name not in get_supported_exchanges():
        raise HTTPException(400, f"Unsupported exchange: {req.exchange}")
    db_user = get_user_by_id(user["sub"]) or {}
    if req.live_trading:
        if not db_user.get("live_trading_allowed", 0):
            raise HTTPException(403, "Live trading is not enabled for your account")
        if not get_user_active_subscription(user["sub"]):
            raise HTTPException(403, "An active subscription is required for live trading")
    current = get_user_settings(user["sub"]).get("exchange") or {}
    updates = {
        "exchange": exchange_name,
        "live_trading": req.live_trading,
        "max_leverage": int(db_user.get("max_leverage") or 20),
        "max_position_pct": float(db_user.get("max_position_pct") or 10.0),
        "api_key": req.api_key or current.get("api_key", ""),
        "api_secret": req.api_secret or current.get("api_secret", ""),
        "password": req.password or current.get("password", ""),
    }
    update_user_settings(user["sub"], {"exchange": updates})
    return {"status": "saved", "exchange": exchange_name}


@app.post("/api/user/settings/take-profit")
async def api_user_save_take_profit(req: UserTakeProfitSettingsRequest, user=Depends(get_current_user)):
    active_qty = [req.tp1_qty, req.tp2_qty, req.tp3_qty, req.tp4_qty][:req.num_levels]
    if any(q <= 0 for q in active_qty):
        raise HTTPException(400, "Active TP close percentages must be greater than 0")
    total_qty = sum(active_qty)
    if total_qty > 100:
        raise HTTPException(400, f"Total active TP close percentage is {total_qty:.2f}%, must be <= 100%")
    update_user_settings(user["sub"], {"take_profit": req.dict()})
    return {"status": "saved", "num_levels": req.num_levels}


@app.get("/api/user/history")
async def api_user_history(days: int = 30, user=Depends(get_current_user)):
    days = max(1, min(days, 365))
    return get_trade_history(days, user_id=user["sub"])


@app.get("/api/user/performance")
async def api_user_performance(days: int = 30, user=Depends(get_current_user)):
    days = max(1, min(days, 365))
    return calculate_performance(days, user_id=user["sub"])


class AdminUserUpdateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    role: str = "user"
    is_active: bool = True
    balance_usdt: float = Field(default=0.0, ge=0.0)
    live_trading_allowed: bool = False
    max_leverage: int = Field(default=20, ge=1, le=125)
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100.0)


class AdminUserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    role: str = "user"
    is_active: bool = True
    balance_usdt: float = Field(default=0.0, ge=0.0)
    live_trading_allowed: bool = False
    max_leverage: int = Field(default=20, ge=1, le=125)
    max_position_pct: float = Field(default=10.0, ge=0.1, le=100.0)


class AdminSubscriptionRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=80)
    duration_days: int = Field(default=0, ge=0, le=3650)
    status: str = "active"


class AdminPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


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


@app.post("/api/admin/users")
async def api_admin_create_user(req: AdminUserCreateRequest, request: Request, admin=Depends(require_admin)):
    ok, reason = validate_password_strength(req.password, username=req.username, email=req.email)
    if not ok:
        raise HTTPException(400, reason)
    try:
        user = create_user_admin(
            username=req.username,
            email=req.email,
            password_hash=hash_password(req.password),
            role=req.role,
            is_active=req.is_active,
            balance_usdt=req.balance_usdt,
            live_trading_allowed=req.live_trading_allowed,
            max_leverage=req.max_leverage,
            max_position_pct=req.max_position_pct,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info(f"[Admin] User {user['username']} created by {admin['username']}")
    _audit(admin, "user.create", "user", user["id"], f"Created user {user['username']}", request)
    return {"status": "created", "user": user}


@app.post("/api/admin/user/{user_id}/toggle")
async def api_admin_toggle_user(user_id: str, request: Request, admin=Depends(require_admin)):
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user_id == admin["sub"]:
        raise HTTPException(400, "You cannot disable your own admin account")
    if user.get("role") == "admin":
        raise HTTPException(400, "Admin accounts cannot be toggled from this endpoint")
    new_status = not bool(user.get("is_active", 1))
    update_user_status(user_id, new_status)
    _audit(admin, "user.toggle", "user", user_id, f"is_active={new_status}", request)
    return {"status": "ok", "is_active": new_status}


@app.put("/api/admin/user/{user_id}")
async def api_admin_update_user(user_id: str, req: AdminUserUpdateRequest, request: Request, admin=Depends(require_admin)):
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
            live_trading_allowed=req.live_trading_allowed,
            max_leverage=req.max_leverage,
            max_position_pct=req.max_position_pct,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(admin, "user.update", "user", user_id, f"Updated {updated['username']}", request)
    return {"status": "updated", "user": updated}


@app.delete("/api/admin/user/{user_id}")
async def api_admin_delete_user(user_id: str, request: Request, admin=Depends(require_admin)):
    if user_id == admin["sub"]:
        raise HTTPException(400, "You cannot delete your own account")
    try:
        deleted = delete_user_admin(user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, "User not found")
    logger.info(f"[Admin] User {user_id} deleted by {admin['username']}")
    _audit(admin, "user.delete", "user", user_id, "Deleted user", request)
    return {"status": "deleted"}


@app.post("/api/admin/user/{user_id}/subscription")
async def api_admin_set_user_subscription(user_id: str, req: AdminSubscriptionRequest, request: Request, admin=Depends(require_admin)):
    if not get_user_by_id(user_id):
        raise HTTPException(404, "User not found")
    if req.status not in ("active", "pending"):
        raise HTTPException(400, "status must be active or pending")
    try:
        sub = set_user_subscription(user_id, req.plan_id, req.status, req.duration_days or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(admin, "subscription.grant", "user", user_id, f"plan={req.plan_id}, status={req.status}", request)
    return {"status": "saved", "subscription": sub}


@app.post("/api/admin/user/{user_id}/password")
async def api_admin_set_user_password(user_id: str, req: AdminPasswordRequest, request: Request, admin=Depends(require_admin)):
    db_user = get_user_by_id(user_id)
    if not db_user:
        raise HTTPException(404, "User not found")
    ok, reason = validate_password_strength(req.password, username=db_user.get("username", ""), email=db_user.get("email", ""))
    if not ok:
        raise HTTPException(400, reason)
    if not update_user_password_hash(user_id, hash_password(req.password)):
        raise HTTPException(404, "User not found")
    logger.info(f"[Admin] Password reset for user {user_id} by {admin['username']}")
    _audit(admin, "user.password_reset", "user", user_id, "Password reset", request)
    return {"status": "saved"}


@app.get("/api/admin/payments")
async def api_admin_payments(status: Optional[str] = None, admin=Depends(require_admin)):
    return get_all_payments(status)


@app.post("/api/admin/payment/{payment_id}/confirm")
async def api_admin_confirm_payment(payment_id: str, request: Request, admin=Depends(require_admin)):
    try:
        confirm_payment(payment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    logger.info(f"[Admin] Payment {payment_id} confirmed")
    _audit(admin, "payment.confirm", "payment", payment_id, "Payment confirmed", request)
    return {"status": "confirmed"}


@app.post("/api/admin/payment/{payment_id}/verify")
async def api_admin_verify_payment(payment_id: str, request: Request, admin=Depends(require_admin)):
    payment = get_payment_by_id(payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    try:
        result = await verify_payment_tx(
            network=payment.get("network", ""),
            tx_hash=payment.get("tx_hash", ""),
            expected_address=payment.get("wallet_address", ""),
            expected_amount=float(payment.get("amount") or 0),
            currency=payment.get("currency", "USDT"),
        )
    except Exception as exc:
        result = {"verified": False, "status": "verifier_error", "reason": f"Verification provider error: {exc}"}
    if result.get("verified"):
        confirm_payment(payment_id)
        _audit(admin, "payment.auto_confirm", "payment", payment_id, result.get("reason", "Payment verified"), request)
        return {"status": "confirmed", "verification": result}
    _audit(admin, "payment.verify", "payment", payment_id, result.get("reason", "Payment not verified"), request)
    return {"status": "not_verified", "verification": result}


@app.post("/api/admin/payment/{payment_id}/reject")
async def api_admin_reject_payment(payment_id: str, request: Request, admin=Depends(require_admin)):
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE payments SET status='rejected' WHERE id=?", (payment_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Payment not found")
    finally:
        conn.close()
    _audit(admin, "payment.reject", "payment", payment_id, "Payment rejected", request)
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
async def api_admin_create_plan(req: PlanRequest, request: Request, admin=Depends(require_admin)):
    plan = create_subscription_plan(req.name, req.description, req.price_usdt, req.duration_days, req.features, req.max_signals_per_day)
    _audit(admin, "plan.create", "plan", plan["id"], f"Created plan {req.name}", request)
    return plan


@app.get("/api/admin/plans")
async def api_admin_get_plans(admin=Depends(require_admin)):
    return get_subscription_plans(active_only=False)


@app.put("/api/admin/plans/{plan_id}")
async def api_admin_update_plan(plan_id: str, req: PlanRequest, request: Request, admin=Depends(require_admin)):
    update_subscription_plan(plan_id, name=req.name, description=req.description, price_usdt=req.price_usdt,
                             duration_days=req.duration_days, features=req.features, max_signals_per_day=req.max_signals_per_day)
    _audit(admin, "plan.update", "plan", plan_id, f"Updated plan {req.name}", request)
    return {"status": "updated"}


@app.delete("/api/admin/plans/{plan_id}")
async def api_admin_delete_plan(plan_id: str, request: Request, admin=Depends(require_admin)):
    delete_subscription_plan(plan_id)
    _audit(admin, "plan.delete", "plan", plan_id, "Plan disabled", request)
    return {"status": "deleted"}


# Admin payment address management
class PaymentAddressRequest(BaseModel):
    network: str = Field(min_length=2, max_length=20)
    address: str = Field(min_length=8, max_length=200)


@app.get("/api/admin/payment-addresses")
async def api_admin_get_addresses(admin=Depends(require_admin)):
    return get_all_payment_addresses()


@app.post("/api/admin/payment-addresses")
async def api_admin_set_address(req: PaymentAddressRequest, request: Request, admin=Depends(require_admin)):
    try:
        set_payment_address(req.network, req.address)
    except ValueError as e:
        raise HTTPException(400, str(e))
    network = req.network.upper().strip()
    _audit(admin, "payment_address.update", "payment_address", network, "Payment address updated", request)
    return {"status": "saved", "network": network}


@app.get("/api/admin/registration")
async def api_admin_registration(admin=Depends(require_admin)):
    return await api_registration_settings()


@app.post("/api/admin/registration")
async def api_admin_save_registration(req: RegistrationSettingsRequest, request: Request, admin=Depends(require_admin)):
    set_admin_setting("registration_invite_required", "true" if req.invite_required else "false")
    _audit(admin, "registration.update", "settings", "registration", f"invite_required={req.invite_required}", request)
    return {"status": "saved", "invite_required": req.invite_required}


@app.get("/api/admin/invite-codes")
async def api_admin_invite_codes(admin=Depends(require_admin)):
    return list_invite_codes()


@app.post("/api/admin/invite-codes")
async def api_admin_create_invite_code(req: InviteCodeRequest, request: Request, admin=Depends(require_admin)):
    code = create_invite_code(req.note, req.max_uses, req.expires_at, admin["sub"])
    _audit(admin, "invite.create", "invite_code", code["code"], "Invite code generated", request)
    return code


@app.get("/api/admin/redeem-codes")
async def api_admin_redeem_codes(admin=Depends(require_admin)):
    return list_redeem_codes()


@app.post("/api/admin/redeem-codes")
async def api_admin_create_redeem_code(req: RedeemCodeCreateRequest, request: Request, admin=Depends(require_admin)):
    if not req.plan_id and req.balance_usdt <= 0:
        raise HTTPException(400, "Choose a subscription plan or set balance_usdt")
    try:
        code = create_redeem_code(
            plan_id=req.plan_id,
            duration_days=req.duration_days,
            balance_usdt=req.balance_usdt,
            note=req.note,
            expires_at=req.expires_at,
            created_by=admin["sub"],
        )
        _audit(admin, "redeem.create", "redeem_code", code["code"], "Card code generated", request)
        return code
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/admin/audit-logs")
async def api_admin_audit_logs(limit: int = 100, admin=Depends(require_admin)):
    return get_admin_audit_logs(limit)


@app.get("/api/admin/webhook-events")
async def api_admin_webhook_events(limit: int = 100, status: str = "", admin=Depends(require_admin)):
    return get_webhook_events(limit=limit, status=status)


@app.get("/api/admin/backups")
async def api_admin_backups(admin=Depends(require_admin)):
    return list_backups()


@app.post("/api/admin/backups")
async def api_admin_create_backup(request: Request, admin=Depends(require_admin)):
    backup = create_backup()
    _audit(admin, "backup.create", "backup", backup["filename"], "Backup created", request)
    return backup


@app.get("/api/admin/backups/{filename}")
async def api_admin_download_backup(filename: str, admin=Depends(require_admin)):
    try:
        path = backup_path(filename)
    except FileNotFoundError:
        raise HTTPException(404, "Backup not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return FileResponse(path, filename=path.name, media_type="application/zip")


@app.post("/api/admin/backups/{filename}/restore")
async def api_admin_stage_restore(filename: str, request: Request, admin=Depends(require_admin)):
    try:
        result = stage_restore(filename)
    except FileNotFoundError:
        raise HTTPException(404, "Backup not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(admin, "backup.stage_restore", "backup", filename, result.get("message", ""), request)
    return result


@app.get("/api/admin/position-monitor")
async def api_admin_position_monitor_state(admin=Depends(require_admin)):
    return get_monitor_state()


@app.post("/api/admin/position-monitor/run")
async def api_admin_run_position_monitor(request: Request, admin=Depends(require_admin)):
    users = get_all_users()
    user_configs = {}
    for u in users:
        cfg = get_user_settings(u["id"])
        if cfg:
            user_configs[u["id"]] = cfg
    result = await run_position_monitor_once(user_configs)
    _audit(admin, "position_monitor.run", "system", "position_monitor", f"checked={result.get('checked')}, adjusted={result.get('adjusted')}", request)
    return result


# ═══════════════════════════════════════════════
# HEALTH & STATUS
# ═══════════════════════════════════════════════

@app.get("/api/status")
async def api_status(response: Response, current_user=Depends(get_optional_user)):
    """
    Public/user: returns minimal server info.
    Admin: returns full configuration status.
    """
    _apply_no_store_headers(response)
    base = {
        "name": "TradingView Signal Server",
        "status": "running",
        "version": "4.0.0",
        "time": datetime.utcnow().isoformat(),
        "public_base_url": _public_base_url(None),
    }
    if not current_user:
        return base
    if current_user.get("role") != "admin":
        return {
            **base,
            "authenticated": True,
            "role": current_user.get("role", "user"),
        }
    ai_key_configured = {
        "openai": bool(settings.ai.openai_api_key),
        "anthropic": bool(settings.ai.anthropic_api_key),
        "deepseek": bool(settings.ai.deepseek_api_key),
        "custom": bool(settings.ai.custom_provider_api_key),
    }
    if settings.ai.provider in {"custom", settings.ai.custom_provider_name}:
        active_ai_key_configured = ai_key_configured["custom"]
    else:
        active_ai_key_configured = ai_key_configured.get(settings.ai.provider, False)
    return {
        **base,
        "ai_provider": settings.ai.provider,
        "exchange": settings.exchange.name,
        "live_trading": settings.exchange.live_trading,
        "exchange_api_configured": bool(settings.exchange.api_key and settings.exchange.api_secret),
        "exchange_password_configured": bool(settings.exchange.password),
        "supported_exchanges": get_supported_exchanges(),
        "tp_levels": settings.take_profit.num_levels,
        "trailing_stop_mode": settings.trailing_stop.mode,
        "ai_temperature": settings.ai.temperature,
        "ai_max_tokens": settings.ai.max_tokens,
        "ai_custom_system_prompt": settings.ai.custom_system_prompt,
        "custom_provider_enabled": settings.ai.custom_provider_enabled,
        "custom_provider_name": settings.ai.custom_provider_name,
        "custom_provider_model": settings.ai.custom_provider_model,
        "custom_provider_url": settings.ai.custom_provider_api_url,
        "ai_api_configured": active_ai_key_configured,
        "ai_keys_configured": ai_key_configured,
        "telegram": {
            "chat_id": settings.telegram.chat_id,
            "bot_configured": bool(settings.telegram.bot_token),
        },
        "risk": {
            "max_position_pct": settings.risk.max_position_pct,
            "max_daily_trades": settings.risk.max_daily_trades,
            "max_daily_loss_pct": settings.risk.max_daily_loss_pct,
            "exit_management_mode": settings.risk.exit_management_mode,
            "ai_risk_profile": settings.risk.ai_risk_profile,
            "custom_stop_loss_pct": settings.risk.custom_stop_loss_pct,
            "ai_exit_system_prompt": settings.risk.ai_exit_system_prompt,
        },
        "take_profit": {
            "num_levels": settings.take_profit.num_levels,
            "tp1_pct": settings.take_profit.tp1_pct,
            "tp2_pct": settings.take_profit.tp2_pct,
            "tp3_pct": settings.take_profit.tp3_pct,
            "tp4_pct": settings.take_profit.tp4_pct,
            "tp1_qty": settings.take_profit.tp1_qty,
            "tp2_qty": settings.take_profit.tp2_qty,
            "tp3_qty": settings.take_profit.tp3_qty,
            "tp4_qty": settings.take_profit.tp4_qty,
        },
        "trailing_stop": {
            "mode": settings.trailing_stop.mode,
            "trail_pct": settings.trailing_stop.trail_pct,
            "activation_profit_pct": settings.trailing_stop.activation_profit_pct,
            "trailing_step_pct": settings.trailing_stop.trailing_step_pct,
        },
    }


def _tradingview_template(secret: str) -> str:
    escaped_secret = json.dumps(secret)
    return (
        "{\n"
        f'  "secret": {escaped_secret},\n'
        '  "ticker": "{{ticker}}",\n'
        '  "exchange": "{{exchange}}",\n'
        '  "direction": "long",\n'
        '  "price": {{close}},\n'
        '  "timeframe": "{{interval}}",\n'
        '  "strategy": "{{strategy.order.comment}}",\n'
        '  "message": "{{strategy.order.action}} {{ticker}} @ {{close}}"\n'
        "}"
    )


@app.get("/api/admin/webhook-config")
async def api_admin_webhook_config(request: Request, admin=Depends(require_admin)):
    webhook_url = _public_base_url(request) + "/webhook"
    return {
        "webhook_url": webhook_url,
        "secret": settings.server.webhook_secret,
        "template": _tradingview_template(settings.server.webhook_secret),
    }


@app.get("/health")
async def health():
    start = time.monotonic()
    db_ok = False
    db_error = ""
    try:
        c = get_connection()
        c.execute("SELECT 1").fetchone()
        c.close()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    data_status = _writable_status(DATA_DIR)
    logs_status = _writable_status(Path(__file__).parent / "logs")
    disk = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else Path(__file__).parent)
    checks = {
        "db": {"ok": db_ok, "latency_ms": latency_ms, **({"error": db_error} if db_error else {})},
        "storage": {"data_writable": data_status.get("writable", False), "logs_writable": logs_status.get("writable", False)},
        "disk": {"free_mb": round(disk.free / 1024 / 1024, 1), "ok": disk.free > 256 * 1024 * 1024},
        "ai": {"configured": bool(settings.ai.provider), "provider": settings.ai.provider},
        "exchange": {"name": settings.exchange.name, "live_trading": settings.exchange.live_trading},
    }
    ok = db_ok and checks["storage"]["data_writable"] and checks["storage"]["logs_writable"] and checks["disk"]["ok"]
    return {"status": "ok" if ok else "degraded", "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    counts = get_operational_counts()
    lines = [
        "# HELP signal_server_users_total Total users.",
        "# TYPE signal_server_users_total gauge",
        f"signal_server_users_total {counts['users']}",
        "# HELP signal_server_active_users_total Active users.",
        "# TYPE signal_server_active_users_total gauge",
        f"signal_server_active_users_total {counts['active_users']}",
        "# HELP signal_server_trades_total Total stored trades.",
        "# TYPE signal_server_trades_total counter",
        f"signal_server_trades_total {counts['trades_total']}",
        "# HELP signal_server_trades_today Trades stored today.",
        "# TYPE signal_server_trades_today gauge",
        f"signal_server_trades_today {counts['trades_today']}",
        "# HELP signal_server_webhook_events_today Webhook events today.",
        "# TYPE signal_server_webhook_events_today gauge",
        f"signal_server_webhook_events_today {counts['webhook_events_today']}",
        "# HELP signal_server_open_positions Open tracked positions.",
        "# TYPE signal_server_open_positions gauge",
        f"signal_server_open_positions {counts['open_positions']}",
    ]
    return "\n".join(lines) + "\n"


def _git_commit() -> str:
    try:
        head = (Path(__file__).parent / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            return (Path(__file__).parent / ".git" / ref).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except Exception:
        return os.getenv("APP_VERSION", "")


def _writable_status(path: Path) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"path": str(path), "writable": True}
    except Exception as e:
        return {"path": str(path), "writable": False, "error": str(e)}


@app.get("/api/admin/system")
async def api_admin_system(request: Request, admin=Depends(require_admin)):
    db_ok = False
    try:
        c = get_connection()
        c.execute("SELECT 1").fetchone()
        c.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "version": "4.0.0",
        "commit": _git_commit(),
        "public_base_url": _public_base_url(request),
        "webhook_url": _public_base_url(request) + "/webhook",
        "live_trading": settings.exchange.live_trading,
        "db": {"ok": db_ok},
        "storage": {
            "data": _writable_status(DATA_DIR),
            "runtime_settings": {**_writable_status(DATA_DIR), "path": str(SETTINGS_FILE), "exists": SETTINGS_FILE.exists()},
            "logs": _writable_status(Path(__file__).parent / "logs"),
            "trade_logs": _writable_status(Path(__file__).parent / "trade_logs"),
        },
        "security": {
            "cookie_secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
            "public_base_url_configured": not _is_placeholder_public_base_url(settings.server.public_base_url),
            "encryption_key_configured": bool(os.getenv("APP_ENCRYPTION_KEY", "")),
        },
    }


# ═══════════════════════════════════════════════
# MAIN WEBHOOK ENDPOINT
# ═══════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    """
    Main webhook endpoint for TradingView alerts.
    Pipeline: Parse → Market Data → Pre-Filter → AI Analysis → Decision → Execute → Log
    """
    fingerprint = ""
    try:
        raw_body = await request.body()
        if not _verify_webhook_signature(request, raw_body):
            logger.warning(f"[Webhook] Invalid HMAC signature from {_client_ip(request)}")
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        try:
            signal = TradingViewSignal(**body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=json.loads(e.json()))

        logger.info(f"[Webhook] Signal: {signal.ticker} {signal.direction.value} @ {signal.price}")
        webhook_user = None
        client_ip = _client_ip(request)
        if settings.server.webhook_secret and hmac.compare_digest(signal.secret, settings.server.webhook_secret):
            pass
        else:
            webhook_user = find_user_by_webhook_secret(signal.secret)
            if not webhook_user:
                safe_body = {k: v for k, v in body.items() if k != "secret"}
                record_webhook_event(
                    fingerprint=_webhook_fingerprint(body, None),
                    status="invalid_secret",
                    status_code=403,
                    ticker=signal.ticker,
                    direction=signal.direction.value,
                    reason="Invalid webhook secret",
                    client_ip=client_ip,
                    payload=safe_body,
                )
                logger.warning(f"[Webhook] Invalid secret from {client_ip}")
                raise HTTPException(status_code=403, detail="Invalid webhook secret")
        user_id = webhook_user["id"] if webhook_user else None
        user_settings = get_user_settings(user_id) if user_id else {}
        if user_id:
            db_user = get_user_by_id(user_id) or {}
            user_settings.setdefault("exchange", {})
            user_settings["exchange"]["max_leverage"] = int(db_user.get("max_leverage") or 20)
            user_settings["exchange"]["max_position_pct"] = float(db_user.get("max_position_pct") or settings.risk.max_position_pct)
            if user_settings["exchange"].get("live_trading") and (
                not db_user.get("live_trading_allowed", 0) or not get_user_active_subscription(user_id)
            ):
                user_settings["exchange"]["live_trading"] = False
        fingerprint = _webhook_fingerprint(body, user_id)
        safe_body = {k: v for k, v in body.items() if k != "secret"}
        if has_recent_webhook_event(fingerprint, minutes=10) or not acquire_webhook_fingerprint(fingerprint, user_id=user_id, ttl_minutes=60):
            record_webhook_event(
                fingerprint=fingerprint,
                status="duplicate",
                status_code=200,
                user_id=user_id,
                ticker=signal.ticker,
                direction=signal.direction.value,
                reason="Duplicate webhook ignored",
                client_ip=client_ip,
                payload=safe_body,
            )
            return JSONResponse(content={"status": "duplicate", "reason": "Duplicate webhook ignored"})

        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        # Fetch market context
        market = await fetch_market_context(signal.ticker)

        # Pre-filter
        filter_result = run_pre_filter(
            signal, market,
            max_daily_trades=settings.risk.max_daily_trades,
            max_daily_loss_pct=settings.risk.max_daily_loss_pct,
            user_id=user_id,
            disabled_checks=_disabled_pre_filter_checks(),
        )

        if not filter_result.passed:
            await notify_pre_filter_blocked(signal.ticker, signal.direction.value, filter_result.reason)
            decision = TradeDecision(
                execute=False, ticker=signal.ticker,
                reason=f"Pre-filter: {filter_result.reason}", signal=signal,
            )
            trade_id = log_trade(decision, {"status": "blocked_by_prefilter"}, user_id=user_id)
            record_webhook_event(
                fingerprint=fingerprint,
                status="blocked",
                status_code=200,
                user_id=user_id,
                ticker=signal.ticker,
                direction=signal.direction.value,
                reason=filter_result.reason,
                client_ip=client_ip,
                payload=safe_body,
            )
            mark_webhook_fingerprint(fingerprint, "blocked")
            return JSONResponse(content={
                "status": "blocked", "trade_id": trade_id,
                "reason": filter_result.reason, "checks": filter_result.checks,
            })

        # AI Analysis
        analysis = await analyze_signal(signal, market)
        await notify_ai_analysis(signal.ticker, analysis)

        # Decision
        decision = _make_decision(signal, analysis, market, user_settings=user_settings)

        # Execute
        order_result = {"status": "not_executed"}
        if decision.execute:
            order_result = await execute_trade(decision, user_settings.get("exchange") if user_settings else None)
            if order_result.get("status") in ("filled", "simulated", "closed"):
                increment_trade_count()
            await notify_trade_executed(decision, order_result)

        trade_id = log_trade(decision, order_result, user_id=user_id)
        invalidate_performance_cache()
        final_status = "executed" if decision.execute else "rejected"
        record_webhook_event(
            fingerprint=fingerprint,
            status=final_status,
            status_code=200,
            user_id=user_id,
            ticker=signal.ticker,
            direction=signal.direction.value,
            reason=decision.reason,
            client_ip=client_ip,
            payload=safe_body,
        )
        mark_webhook_fingerprint(fingerprint, final_status)

        return JSONResponse(content={
            "status": final_status,
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
        if fingerprint:
            mark_webhook_fingerprint(fingerprint, "error")
        # Sanitize: never leak internal exception text to callers
        await notify_error(f"Pipeline error: {e}")
        raise HTTPException(status_code=500, detail="Internal pipeline error. Check server logs.")


# ─────────────────────────────────────────────
# Decision logic
# ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.5
RISK_THRESHOLD = 0.8


def _make_decision(signal, analysis, market, user_settings: dict | None = None) -> TradeDecision:
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

    stop_loss = _build_stop_loss(analysis, entry, direction, user_settings=user_settings)
    size_multiplier = _clamp(analysis.position_size_pct, 0.0, 1.0)
    qty = _calc_qty(entry, stop_loss, market, user_settings=user_settings) * size_multiplier
    if qty <= 0:
        return TradeDecision(
            execute=False, ticker=signal.ticker,
            reason="Calculated quantity is zero",
            signal=signal, ai_analysis=analysis,
        )

    tp_levels = _build_tp_levels(analysis, entry, direction, user_settings=user_settings)
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


def _build_tp_levels(analysis, entry, direction, user_settings: dict | None = None) -> list[TakeProfitLevel]:
    tp_levels = []
    tp_cfg = (user_settings or {}).get("take_profit") or {}
    num = int(_clamp(tp_cfg.get("num_levels", settings.take_profit.num_levels), 1, 4))
    is_long = direction in (SignalDirection.LONG,)

    for i in range(1, num + 1):
        ai_tp = getattr(analysis, f"suggested_tp{i}", None)
        ai_qty = getattr(analysis, f"tp{i}_qty_pct", 25.0)
        default_pct = tp_cfg.get(f"tp{i}_pct", getattr(settings.take_profit, f"tp{i}_pct", 2.0 * i))
        default_qty = tp_cfg.get(f"tp{i}_qty", getattr(settings.take_profit, f"tp{i}_qty", 25.0))

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


def _build_stop_loss(analysis, entry, direction, user_settings: dict | None = None) -> float | None:
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


def _calc_qty(entry, stop_loss, market, risk_pct=1.0, user_settings: dict | None = None):
    if not entry or entry <= 0:
        return 0.0
    account_equity = max(settings.risk.account_equity_usdt, 0)
    if account_equity <= 0:
        return 0.0
    max_position_pct = ((user_settings or {}).get("exchange") or {}).get("max_position_pct", settings.risk.max_position_pct)
    if stop_loss and stop_loss > 0:
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit > 0:
            risk_capital = account_equity * risk_pct * 0.01
            max_qty = (account_equity * float(max_position_pct) * 0.01) / entry
            return min(risk_capital / risk_per_unit, max_qty)
    return (account_equity * float(max_position_pct) * 0.01) / entry


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
    ai_risk_profile: str = "balanced"
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
async def save_exchange_settings(req: ExchangeSettingsRequest, request: Request, admin=Depends(require_admin)):
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
    _save_runtime_settings({"exchange": {
        "name": settings.exchange.name,
        "api_key": settings.exchange.api_key,
        "api_secret": settings.exchange.api_secret,
        "password": settings.exchange.password,
    }})
    logger.info(f"[Settings] Exchange updated: {settings.exchange.name}")
    _audit(admin, "settings.exchange", "settings", "exchange", f"exchange={settings.exchange.name}", request)
    return {"status": "saved", "exchange": settings.exchange.name}


@app.post("/api/settings/ai")
async def save_ai_settings(req: AISettingsRequest, request: Request, admin=Depends(require_admin)):
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
            "openai_api_key": settings.ai.openai_api_key,
            "anthropic_api_key": settings.ai.anthropic_api_key,
            "deepseek_api_key": settings.ai.deepseek_api_key,
            "custom_provider_api_key": settings.ai.custom_provider_api_key,
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
    _audit(admin, "settings.ai", "settings", "ai", f"provider={settings.ai.provider}", request)
    return {"status": "saved", "provider": settings.ai.provider}


@app.post("/api/settings/telegram")
async def save_telegram_settings(req: TelegramSettingsRequest, request: Request, admin=Depends(require_admin)):
    if req.bot_token:
        settings.telegram.bot_token = req.bot_token
    if req.chat_id:
        settings.telegram.chat_id = req.chat_id
    _save_runtime_settings({"telegram": {
        "bot_token": settings.telegram.bot_token,
        "chat_id": settings.telegram.chat_id,
    }})
    logger.info("[Settings] Telegram updated")
    _audit(admin, "settings.telegram", "settings", "telegram", "Telegram settings updated", request)
    return {"status": "saved"}


@app.post("/api/settings/risk")
async def save_risk_settings(req: RiskSettingsRequest, request: Request, admin=Depends(require_admin)):
    if req.exit_management_mode not in ("ai", "custom"):
        raise HTTPException(400, "exit_management_mode must be ai or custom")
    if req.ai_risk_profile not in ("conservative", "balanced", "aggressive"):
        raise HTTPException(400, "ai_risk_profile must be conservative, balanced, or aggressive")
    settings.risk.max_position_pct = req.max_position_pct
    settings.risk.max_daily_trades = req.max_daily_trades
    settings.risk.max_daily_loss_pct = req.max_daily_loss_pct
    settings.risk.exit_management_mode = req.exit_management_mode
    settings.risk.ai_risk_profile = req.ai_risk_profile
    settings.risk.custom_stop_loss_pct = req.custom_stop_loss_pct
    settings.risk.ai_exit_system_prompt = req.ai_exit_system_prompt
    _save_runtime_settings({
        "risk": {
            "max_position_pct": req.max_position_pct,
            "max_daily_trades": req.max_daily_trades,
            "max_daily_loss_pct": req.max_daily_loss_pct,
            "exit_management_mode": req.exit_management_mode,
            "ai_risk_profile": req.ai_risk_profile,
            "custom_stop_loss_pct": req.custom_stop_loss_pct,
            "ai_exit_system_prompt": req.ai_exit_system_prompt,
        }
    })
    logger.info("[Settings] Risk settings updated")
    _audit(admin, "settings.risk", "settings", "risk", f"profile={req.ai_risk_profile}, mode={req.exit_management_mode}", request)
    return {"status": "saved"}


@app.post("/api/settings/take-profit")
async def save_take_profit_settings(req: TakeProfitSettingsRequest, request: Request, admin=Depends(require_admin)):
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
    _audit(admin, "settings.take_profit", "settings", "take_profit", f"levels={req.num_levels}", request)
    return {"status": "saved", "num_levels": req.num_levels}


@app.post("/api/settings/trailing-stop")
async def save_trailing_stop_settings(req: TrailingStopSettingsRequest, request: Request, admin=Depends(require_admin)):
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
    _audit(admin, "settings.trailing_stop", "settings", "trailing_stop", f"mode={req.mode}", request)
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
        max_daily_loss_pct=settings.risk.max_daily_loss_pct,
        user_id=None,
        disabled_checks=_disabled_pre_filter_checks())
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
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=os.getenv("UVICORN_RELOAD", "false").lower() == "true",
    )

"""
Signal Server - Configuration (Enhanced)
Pydantic Settings with validation and type safety.
"""
import os
import secrets
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)


class AIConfig(BaseModel):
    """AI provider configuration."""
    provider: str = os.getenv("AI_PROVIDER", "deepseek")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    custom_provider_enabled: bool = os.getenv("CUSTOM_AI_PROVIDER_ENABLED", "false").lower() == "true"
    custom_provider_name: str = os.getenv("CUSTOM_AI_PROVIDER_NAME", "custom")
    custom_provider_api_key: str = os.getenv("CUSTOM_AI_API_KEY", "")
    custom_provider_model: str = os.getenv("CUSTOM_AI_MODEL", "")
    custom_provider_api_url: str = os.getenv("CUSTOM_AI_API_URL", "")
    temperature: float = float(os.getenv("AI_TEMPERATURE", "0.3"))
    max_tokens: int = int(os.getenv("AI_MAX_TOKENS", "1000"))
    custom_system_prompt: str = os.getenv("AI_CUSTOM_PROMPT", "")
    connect_timeout_secs: float = float(os.getenv("AI_CONNECT_TIMEOUT_SECS", "10"))
    read_timeout_secs: float = float(os.getenv("AI_READ_TIMEOUT_SECS", "90"))
    write_timeout_secs: float = float(os.getenv("AI_WRITE_TIMEOUT_SECS", "30"))
    pool_timeout_secs: float = float(os.getenv("AI_POOL_TIMEOUT_SECS", "10"))
    max_retries: int = int(os.getenv("AI_MAX_RETRIES", "3"))

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {'openai', 'anthropic', 'deepseek', 'custom'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"AI provider must be one of: {allowed}")
        return normalized


class ExchangeConfig(BaseModel):
    """Exchange configuration."""
    name: str = os.getenv("EXCHANGE", "binance")
    api_key: str = os.getenv("EXCHANGE_API_KEY", "")
    api_secret: str = os.getenv("EXCHANGE_API_SECRET", "")
    password: str = os.getenv("EXCHANGE_PASSWORD", "")
    live_trading: bool = os.getenv("LIVE_TRADING", "false").lower() == "true"
    sandbox_mode: bool = os.getenv("EXCHANGE_SANDBOX_MODE", "false").lower() == "true"

    @field_validator('name')
    @classmethod
    def validate_exchange(cls, v: str) -> str:
        allowed = {'binance', 'okx', 'bybit', 'bitget', 'gate', 'coinbase'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"Exchange must be one of: {allowed}")
        return normalized


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


class RiskConfig(BaseModel):
    """Risk management configuration."""
    account_equity_usdt: float = float(os.getenv("ACCOUNT_EQUITY_USDT", "10000"))
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "10.0"))
    max_daily_trades: int = int(os.getenv("MAX_DAILY_TRADES", "10"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
    exit_management_mode: str = os.getenv("EXIT_MANAGEMENT_MODE", "ai")
    ai_risk_profile: str = os.getenv("AI_RISK_PROFILE", "balanced")
    custom_stop_loss_pct: float = float(os.getenv("CUSTOM_STOP_LOSS_PCT", "1.5"))
    ai_exit_system_prompt: str = os.getenv("AI_EXIT_SYSTEM_PROMPT", "")

    @field_validator('exit_management_mode')
    @classmethod
    def validate_exit_mode(cls, v: str) -> str:
        if v not in ('ai', 'custom'):
            raise ValueError("exit_management_mode must be 'ai' or 'custom'")
        return v

    @field_validator('ai_risk_profile')
    @classmethod
    def validate_risk_profile(cls, v: str) -> str:
        if v not in ('conservative', 'balanced', 'aggressive'):
            raise ValueError("ai_risk_profile must be 'conservative', 'balanced', or 'aggressive'")
        return v


class TakeProfitSettings(BaseModel):
    """Take-profit configuration."""
    num_levels: int = int(os.getenv("TP_LEVELS", "1"))
    tp1_pct: float = float(os.getenv("TP1_PCT", "2.0"))
    tp2_pct: float = float(os.getenv("TP2_PCT", "4.0"))
    tp3_pct: float = float(os.getenv("TP3_PCT", "6.0"))
    tp4_pct: float = float(os.getenv("TP4_PCT", "10.0"))
    tp1_qty: float = float(os.getenv("TP1_QTY", "25.0"))
    tp2_qty: float = float(os.getenv("TP2_QTY", "25.0"))
    tp3_qty: float = float(os.getenv("TP3_QTY", "25.0"))
    tp4_qty: float = float(os.getenv("TP4_QTY", "25.0"))

    @field_validator('num_levels')
    @classmethod
    def validate_num_levels(cls, v: int) -> int:
        if not 1 <= v <= 4:
            raise ValueError("num_levels must be between 1 and 4")
        return v


class TrailingStopSettings(BaseModel):
    """Trailing-stop configuration."""
    mode: str = os.getenv("TRAILING_STOP_MODE", "none")
    trail_pct: float = float(os.getenv("TRAILING_STOP_PCT", "1.0"))
    activation_profit_pct: float = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))
    trailing_step_pct: float = float(os.getenv("TRAILING_STEP_PCT", "0.5"))

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {'none', 'moving', 'breakeven_on_tp1', 'step_trailing', 'profit_pct_trailing'}
        if v not in allowed:
            raise ValueError(f"mode must be one of: {allowed}")
        return v


class ServerConfig(BaseModel):
    """Server configuration."""
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    cors_origins: list[str] = ["*"]
    trusted_hosts: list[str] = ["*"]


class DatabaseConfig(BaseModel):
    """Database configuration."""
    url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/server.db")
    pool_size: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    max_overflow: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "10"))
    echo: bool = os.getenv("DATABASE_ECHO", "false").lower() == "true"


class RedisConfig(BaseModel):
    """Redis cache configuration."""
    url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    enabled: bool = os.getenv("REDIS_ENABLED", "false").lower() == "true"
    ttl: int = int(os.getenv("REDIS_TTL", "300"))


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    login_max_attempts: int = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
    login_window_secs: int = int(os.getenv("LOGIN_WINDOW_SECS", "300"))
    register_max_attempts: int = int(os.getenv("REGISTER_MAX_ATTEMPTS", "5"))
    register_window_secs: int = int(os.getenv("REGISTER_WINDOW_SECS", "600"))
    api_default_limit: str = os.getenv("API_DEFAULT_LIMIT", "60/minute")


class Settings(BaseSettings):
    """Application settings."""
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore',
    )

    app_name: str = os.getenv("APP_NAME", "TradingView Signal Server")
    app_version: str = "4.1.0"
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    json_logs: bool = os.getenv("JSON_LOGS", "false").lower() == "true"

    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    cookie_secure: str = os.getenv("COOKIE_SECURE", "auto")
    webhook_hmac_secret: str = os.getenv("WEBHOOK_HMAC_SECRET", "")

    app_encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")

    default_admin_username: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_email: str = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost")
    default_admin_password: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "123456")

    position_monitor_interval_secs: int = int(os.getenv("POSITION_MONITOR_INTERVAL_SECS", "60"))

    ai: AIConfig = AIConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    telegram: TelegramConfig = TelegramConfig()
    risk: RiskConfig = RiskConfig()
    take_profit: TakeProfitSettings = TakeProfitSettings()
    trailing_stop: TrailingStopSettings = TrailingStopSettings()
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    redis: RedisConfig = RedisConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()

    @computed_field
    @property
    def is_production(self) -> bool:
        return self.exchange.live_trading

    def __init__(self, **data):
        super().__init__(**data)
        self._validate_production_settings()

    def _validate_production_settings(self):
        if self.exchange.live_trading:
            if not self.jwt_secret:
                raise RuntimeError("JWT_SECRET must be set when LIVE_TRADING=true")
            if not self.exchange.api_key or not self.exchange.api_secret:
                raise RuntimeError("Exchange API credentials required for live trading")


settings = Settings()

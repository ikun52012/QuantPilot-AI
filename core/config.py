"""
Signal Server - Configuration (Enhanced)
Pydantic Settings with validation and type safety.
"""
import os
import secrets
from pathlib import Path
from typing import Optional, Any
from pydantic import BaseModel, field_validator, model_validator
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH, override=False)


class AIConfig(BaseModel):
    """AI provider configuration."""
    provider: str = "deepseek"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    custom_provider_enabled: bool = False
    custom_provider_name: str = "custom"
    custom_provider_api_key: str = ""
    custom_provider_model: str = ""
    custom_provider_api_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 1000
    custom_system_prompt: str = ""
    connect_timeout_secs: float = 10.0
    read_timeout_secs: float = 90.0
    write_timeout_secs: float = 30.0
    pool_timeout_secs: float = 10.0
    max_retries: int = 3

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {'openai', 'anthropic', 'deepseek', 'custom'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"AI provider must be one of: {allowed}")
        return normalized

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            provider=os.getenv("AI_PROVIDER", "deepseek"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            custom_provider_enabled=os.getenv("CUSTOM_AI_PROVIDER_ENABLED", "false").lower() == "true",
            custom_provider_name=os.getenv("CUSTOM_AI_PROVIDER_NAME", "custom"),
            custom_provider_api_key=os.getenv("CUSTOM_AI_API_KEY", ""),
            custom_provider_model=os.getenv("CUSTOM_AI_MODEL", ""),
            custom_provider_api_url=os.getenv("CUSTOM_AI_API_URL", ""),
            temperature=float(os.getenv("AI_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "1000")),
            custom_system_prompt=os.getenv("AI_CUSTOM_PROMPT", ""),
            connect_timeout_secs=float(os.getenv("AI_CONNECT_TIMEOUT_SECS", "10")),
            read_timeout_secs=float(os.getenv("AI_READ_TIMEOUT_SECS", "90")),
            write_timeout_secs=float(os.getenv("AI_WRITE_TIMEOUT_SECS", "30")),
            pool_timeout_secs=float(os.getenv("AI_POOL_TIMEOUT_SECS", "10")),
            max_retries=int(os.getenv("AI_MAX_RETRIES", "3")),
        )


class ExchangeConfig(BaseModel):
    """Exchange configuration."""
    name: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    password: str = ""
    live_trading: bool = False
    sandbox_mode: bool = False

    @field_validator('name')
    @classmethod
    def validate_exchange(cls, v: str) -> str:
        allowed = {'binance', 'okx', 'bybit', 'bitget', 'gate', 'coinbase'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"Exchange must be one of: {allowed}")
        return normalized

    @classmethod
    def from_env(cls) -> "ExchangeConfig":
        return cls(
            name=os.getenv("EXCHANGE", "binance"),
            api_key=os.getenv("EXCHANGE_API_KEY", ""),
            api_secret=os.getenv("EXCHANGE_API_SECRET", ""),
            password=os.getenv("EXCHANGE_PASSWORD", ""),
            live_trading=os.getenv("LIVE_TRADING", "false").lower() == "true",
            sandbox_mode=os.getenv("EXCHANGE_SANDBOX_MODE", "false").lower() == "true",
        )


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""
    bot_token: str = ""
    chat_id: str = ""

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )


class RiskConfig(BaseModel):
    """Risk management configuration."""
    account_equity_usdt: float = 10000.0
    max_position_pct: float = 10.0
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 5.0
    exit_management_mode: str = "ai"
    ai_risk_profile: str = "balanced"
    custom_stop_loss_pct: float = 1.5
    ai_exit_system_prompt: str = ""

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

    @classmethod
    def from_env(cls) -> "RiskConfig":
        return cls(
            account_equity_usdt=float(os.getenv("ACCOUNT_EQUITY_USDT", "10000")),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "10.0")),
            max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "10")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0")),
            exit_management_mode=os.getenv("EXIT_MANAGEMENT_MODE", "ai"),
            ai_risk_profile=os.getenv("AI_RISK_PROFILE", "balanced"),
            custom_stop_loss_pct=float(os.getenv("CUSTOM_STOP_LOSS_PCT", "1.5")),
            ai_exit_system_prompt=os.getenv("AI_EXIT_SYSTEM_PROMPT", ""),
        )


class TakeProfitSettings(BaseModel):
    """Take-profit configuration."""
    num_levels: int = 1
    tp1_pct: float = 2.0
    tp2_pct: float = 4.0
    tp3_pct: float = 6.0
    tp4_pct: float = 10.0
    tp1_qty: float = 25.0
    tp2_qty: float = 25.0
    tp3_qty: float = 25.0
    tp4_qty: float = 25.0

    @field_validator('num_levels')
    @classmethod
    def validate_num_levels(cls, v: int) -> int:
        if not 1 <= v <= 4:
            raise ValueError("num_levels must be between 1 and 4")
        return v

    @classmethod
    def from_env(cls) -> "TakeProfitSettings":
        return cls(
            num_levels=int(os.getenv("TP_LEVELS", "1")),
            tp1_pct=float(os.getenv("TP1_PCT", "2.0")),
            tp2_pct=float(os.getenv("TP2_PCT", "4.0")),
            tp3_pct=float(os.getenv("TP3_PCT", "6.0")),
            tp4_pct=float(os.getenv("TP4_PCT", "10.0")),
            tp1_qty=float(os.getenv("TP1_QTY", "25.0")),
            tp2_qty=float(os.getenv("TP2_QTY", "25.0")),
            tp3_qty=float(os.getenv("TP3_QTY", "25.0")),
            tp4_qty=float(os.getenv("TP4_QTY", "25.0")),
        )


class TrailingStopSettings(BaseModel):
    """Trailing-stop configuration."""
    mode: str = "none"
    trail_pct: float = 1.0
    activation_profit_pct: float = 1.0
    trailing_step_pct: float = 0.5

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {'none', 'moving', 'breakeven_on_tp1', 'step_trailing', 'profit_pct_trailing'}
        if v not in allowed:
            raise ValueError(f"mode must be one of: {allowed}")
        return v

    @classmethod
    def from_env(cls) -> "TrailingStopSettings":
        return cls(
            mode=os.getenv("TRAILING_STOP_MODE", "none"),
            trail_pct=float(os.getenv("TRAILING_STOP_PCT", "1.0")),
            activation_profit_pct=float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0")),
            trailing_step_pct=float(os.getenv("TRAILING_STEP_PCT", "0.5")),
        )


class ServerConfig(BaseModel):
    """Server configuration."""
    webhook_secret: str = ""
    public_base_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]
    trusted_hosts: list[str] = ["*"]

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


class DatabaseConfig(BaseModel):
    """Database configuration."""
    url: str = "sqlite+aiosqlite:///./data/server.db"
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/server.db"),
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "10")),
            echo=os.getenv("DATABASE_ECHO", "false").lower() == "true",
        )


class RedisConfig(BaseModel):
    """Redis cache configuration."""
    url: str = "redis://localhost:6379/0"
    enabled: bool = False
    ttl: int = 300

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            enabled=os.getenv("REDIS_ENABLED", "false").lower() == "true",
            ttl=int(os.getenv("REDIS_TTL", "300")),
        )


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    enabled: bool = True
    login_max_attempts: int = 10
    login_window_secs: int = 300
    register_max_attempts: int = 5
    register_window_secs: int = 600
    api_default_limit: str = "60/minute"

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        return cls(
            enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "10")),
            login_window_secs=int(os.getenv("LOGIN_WINDOW_SECS", "300")),
            register_max_attempts=int(os.getenv("REGISTER_MAX_ATTEMPTS", "5")),
            register_window_secs=int(os.getenv("REGISTER_WINDOW_SECS", "600")),
            api_default_limit=os.getenv("API_DEFAULT_LIMIT", "60/minute"),
        )


class Settings(BaseModel):
    """Application settings - loaded entirely from environment variables."""
    app_name: str = "TradingView Signal Server"
    app_version: str = "4.1.0"
    debug: bool = False
    json_logs: bool = False

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    cookie_secure: str = "auto"
    webhook_hmac_secret: str = ""

    app_encryption_key: str = ""

    default_admin_username: str = "admin"
    default_admin_email: str = "admin@localhost"
    default_admin_password: str = "123456"

    position_monitor_interval_secs: int = 60

    ai: AIConfig = None  # type: ignore[assignment]
    exchange: ExchangeConfig = None  # type: ignore[assignment]
    telegram: TelegramConfig = None  # type: ignore[assignment]
    risk: RiskConfig = None  # type: ignore[assignment]
    take_profit: TakeProfitSettings = None  # type: ignore[assignment]
    trailing_stop: TrailingStopSettings = None  # type: ignore[assignment]
    server: ServerConfig = None  # type: ignore[assignment]
    database: DatabaseConfig = None  # type: ignore[assignment]
    redis: RedisConfig = None  # type: ignore[assignment]
    rate_limit: RateLimitConfig = None  # type: ignore[assignment]

    model_config = {"arbitrary_types_allowed": True}

    @property
    def is_production(self) -> bool:
        return self.exchange.live_trading

    def _validate_production_settings(self):
        if self.exchange.live_trading:
            if not self.jwt_secret:
                raise RuntimeError("JWT_SECRET must be set when LIVE_TRADING=true")
            if not self.exchange.api_key or not self.exchange.api_secret:
                raise RuntimeError("Exchange API credentials required for live trading")

    @classmethod
    def from_env(cls) -> "Settings":
        """Create Settings instance from environment variables."""
        instance = cls(
            app_name=os.getenv("APP_NAME", "TradingView Signal Server"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            json_logs=os.getenv("JSON_LOGS", "false").lower() == "true",
            jwt_secret=os.getenv("JWT_SECRET", ""),
            cookie_secure=os.getenv("COOKIE_SECURE", "auto"),
            webhook_hmac_secret=os.getenv("WEBHOOK_HMAC_SECRET", ""),
            app_encryption_key=os.getenv("APP_ENCRYPTION_KEY", ""),
            default_admin_username=os.getenv("DEFAULT_ADMIN_USERNAME", "admin"),
            default_admin_email=os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost"),
            default_admin_password=os.getenv("DEFAULT_ADMIN_PASSWORD", "123456"),
            position_monitor_interval_secs=int(os.getenv("POSITION_MONITOR_INTERVAL_SECS", "60")),
            ai=AIConfig.from_env(),
            exchange=ExchangeConfig.from_env(),
            telegram=TelegramConfig.from_env(),
            risk=RiskConfig.from_env(),
            take_profit=TakeProfitSettings.from_env(),
            trailing_stop=TrailingStopSettings.from_env(),
            server=ServerConfig.from_env(),
            database=DatabaseConfig.from_env(),
            redis=RedisConfig.from_env(),
            rate_limit=RateLimitConfig.from_env(),
        )
        instance._validate_production_settings()
        return instance


settings = Settings.from_env()

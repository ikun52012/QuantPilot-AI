"""
QuantPilot AI - Configuration
Pydantic Settings with validation and type safety.
"""
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH, override=False)


def _json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if isinstance(default, list):
            return [item.strip() for item in raw.split(",") if item.strip()]
        return default


class AIConfig(BaseModel):
    """AI provider configuration."""
    provider: str = "deepseek"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.5"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"

    custom_provider_enabled: bool = False
    custom_provider_name: str = "custom"
    custom_provider_api_key: str = ""
    custom_provider_model: str = ""
    custom_provider_api_url: str = ""
    openrouter_enabled: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-5.5"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "QuantPilot AI"
    mistral_api_key: str = ""
    mistral_model: str = "mistral-large-latest"
    temperature: float = 0.3
    max_tokens: int = 1000
    custom_system_prompt: str = ""
    connect_timeout_secs: float = 10.0
    read_timeout_secs: float = 60.0
    write_timeout_secs: float = 30.0
    pool_timeout_secs: float = 10.0
    max_retries: int = 3
    max_concurrent_calls: int = 5
    signal_queue_limit: int = 50
    global_processing_semaphore: int = 5
    signal_processing_interval_secs: float = 1.0
    dynamic_interval_enabled: bool = True
    dynamic_interval_high_load_threshold: float = 30.0
    dynamic_interval_high_load_multiplier: float = 2.0
    priority_skip_interval_confidence_threshold: float = 0.85
    dynamic_cache_ttl_enabled: bool = True
    dynamic_cache_ttl_base: int = 60
    dynamic_cache_ttl_high_volatility_multiplier: float = 0.5
    dynamic_cache_ttl_low_volatility_multiplier: float = 2.0
    batch_signals_enabled: bool = False
    batch_signals_window_secs: float = 5.0
    batch_signals_max_count: int = 3
    prefetch_market_data: bool = True
    websocket_market_data_enabled: bool = False
    voting_enabled: bool = False
    voting_models: list[str] = Field(default_factory=list)
    voting_weights: dict[str, float] = Field(default_factory=dict)
    voting_strategy: str = "weighted"
    available_models: dict[str, list[str]] = Field(default_factory=lambda: {
        "openai": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"],
        "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "deepseek": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "mistral": ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
        "openrouter": [
            "openai/gpt-5.5",
            "openai/gpt-5.4-mini",
            "anthropic/claude-opus-4-7",
            "deepseek/deepseek-v4-pro",
            "google/gemini-pro-1.5",
            "meta-llama/llama-3.1-70b-instruct",
            "mistralai/mistral-large",
            "qwen/qwen-2.5-72b-instruct",
        ],
        "custom": [],
    })

    @field_validator('provider')
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {'openai', 'anthropic', 'deepseek', 'openrouter', 'custom', 'mistral'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"AI provider must be one of: {allowed}")
        return normalized

    @field_validator('voting_strategy')
    @classmethod
    def validate_voting_strategy(cls, v: str) -> str:
        normalized = v.lower().strip()
        allowed = {'weighted', 'consensus', 'best_confidence'}
        if normalized not in allowed:
            raise ValueError(f"voting_strategy must be one of: {allowed}")
        return normalized

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            provider=os.getenv("AI_PROVIDER", "deepseek"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            custom_provider_enabled=os.getenv("CUSTOM_AI_PROVIDER_ENABLED", "false").lower() == "true",
            custom_provider_name=os.getenv("CUSTOM_AI_PROVIDER_NAME", "custom"),
            custom_provider_api_key=os.getenv("CUSTOM_AI_API_KEY", ""),
            custom_provider_model=os.getenv("CUSTOM_AI_MODEL", ""),
            custom_provider_api_url=os.getenv("CUSTOM_AI_API_URL", ""),
            openrouter_enabled=os.getenv("OPENROUTER_ENABLED", "false").lower() == "true",
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-5.5"),
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL", ""),
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "QuantPilot AI"),
            mistral_api_key=os.getenv("MISTRAL_API_KEY", ""),
            mistral_model=os.getenv("MISTRAL_MODEL", "mistral-large-latest"),
            temperature=float(os.getenv("AI_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "1000")),
            custom_system_prompt=os.getenv("AI_CUSTOM_PROMPT", ""),
            connect_timeout_secs=float(os.getenv("AI_CONNECT_TIMEOUT_SECS", "10")),
            read_timeout_secs=float(os.getenv("AI_READ_TIMEOUT_SECS", "90")),
            write_timeout_secs=float(os.getenv("AI_WRITE_TIMEOUT_SECS", "30")),
            pool_timeout_secs=float(os.getenv("AI_POOL_TIMEOUT_SECS", "10")),
            max_retries=int(os.getenv("AI_MAX_RETRIES", "3")),
            voting_enabled=os.getenv("AI_VOTING_ENABLED", "false").lower() == "true",
            voting_models=_json_env("AI_VOTING_MODELS", []),
            voting_weights=_json_env("AI_VOTING_WEIGHTS", {}),
            voting_strategy=os.getenv("AI_VOTING_STRATEGY", "weighted"),
        )


class ExchangeConfig(BaseModel):
    """Exchange configuration."""
    name: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    password: str = ""
    live_trading: bool = False
    sandbox_mode: bool = False
    market_type: str = "contract"
    default_order_type: str = "limit"
    stop_loss_order_type: str = "market"
    limit_timeout_overrides: dict[str, int] = Field(default_factory=dict)
    pool_max_size: int = 50

    @field_validator('name')
    @classmethod
    def validate_exchange(cls, v: str) -> str:
        allowed = {'binance', 'okx', 'bybit', 'bitget', 'gate', 'coinbase'}
        normalized = v.lower().strip()
        if normalized not in allowed:
            raise ValueError(f"Exchange must be one of: {allowed}")
        return normalized

    @field_validator('market_type')
    @classmethod
    def validate_market_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in {'spot', 'contract'}:
            raise ValueError("market_type must be 'spot' or 'contract'")
        return normalized

    @field_validator('default_order_type')
    @classmethod
    def validate_default_order_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in {'market', 'limit'}:
            raise ValueError("default_order_type must be 'market' or 'limit'")
        return normalized

    @field_validator('stop_loss_order_type')
    @classmethod
    def validate_stop_loss_order_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in {'market'}:
            raise ValueError("stop_loss_order_type must be 'market'")
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
            market_type=os.getenv("EXCHANGE_MARKET_TYPE", "contract"),
            default_order_type=os.getenv("EXCHANGE_DEFAULT_ORDER_TYPE", "limit"),
            stop_loss_order_type=os.getenv("EXCHANGE_STOP_LOSS_ORDER_TYPE", "market"),
            limit_timeout_overrides=_json_env("EXCHANGE_LIMIT_TIMEOUT_OVERRIDES", {}),
            pool_max_size=int(os.getenv("EXCHANGE_POOL_MAX_SIZE", "50")),
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
    # Position sizing mode: percentage, fixed, risk_ratio
    position_sizing_mode: str = "percentage"
    # Fixed amount per trade (USDT) - used when mode is 'fixed'
    fixed_position_size_usdt: float = 100.0
    # Risk ratio per trade (percentage of account to risk) - used when mode is 'risk_ratio'
    risk_per_trade_pct: float = 1.0
    # Correlation risk limits
    max_same_direction_positions: int = 5  # Max positions in same direction
    max_correlated_exposure_pct: float = 50.0  # Max % of equity in one direction
    # Production safety mode: live trading should stop when required market/risk data is unavailable.
    live_data_quality_mode: str = "fail_closed"
    max_live_missing_data_checks: int = 0
    block_live_on_risk_check_error: bool = True

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

    @field_validator('position_sizing_mode')
    @classmethod
    def validate_position_sizing_mode(cls, v: str) -> str:
        if v not in ('percentage', 'fixed', 'risk_ratio'):
            raise ValueError("position_sizing_mode must be 'percentage', 'fixed', or 'risk_ratio'")
        return v

    @field_validator('live_data_quality_mode')
    @classmethod
    def validate_live_data_quality_mode(cls, v: str) -> str:
        normalized = str(v or "fail_closed").lower().strip()
        if normalized not in ('fail_closed', 'warn'):
            raise ValueError("live_data_quality_mode must be 'fail_closed' or 'warn'")
        return normalized

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
            position_sizing_mode=os.getenv("POSITION_SIZING_MODE", "percentage"),
            fixed_position_size_usdt=float(os.getenv("FIXED_POSITION_SIZE_USDT", "100")),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),
            max_same_direction_positions=int(os.getenv("MAX_SAME_DIRECTION_POSITIONS", "5")),
            max_correlated_exposure_pct=float(os.getenv("MAX_CORRELATED_EXPOSURE_PCT", "50.0")),
            live_data_quality_mode=os.getenv("LIVE_DATA_QUALITY_MODE", "fail_closed"),
            max_live_missing_data_checks=int(os.getenv("MAX_LIVE_MISSING_DATA_CHECKS", "0")),
            block_live_on_risk_check_error=os.getenv("BLOCK_LIVE_ON_RISK_CHECK_ERROR", "true").lower() == "true",
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
    trust_proxy_headers: bool = False

    @classmethod
    def from_env(cls) -> "ServerConfig":
        cors_raw = os.getenv("CORS_ORIGINS", "")
        cors_origins = [s.strip() for s in cors_raw.split(",") if s.strip()] if cors_raw else ["*"]
        trusted_raw = os.getenv("TRUSTED_HOSTS", "")
        trusted_hosts = [s.strip() for s in trusted_raw.split(",") if s.strip()] if trusted_raw else ["*"]
        return cls(
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            cors_origins=cors_origins,
            trusted_hosts=trusted_hosts,
            trust_proxy_headers=os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true",
        )


class DatabaseConfig(BaseModel):
    """Database configuration."""
    url: str = "sqlite+aiosqlite:///./data/server.db"
    pool_size: int = 15
    max_overflow: int = 20
    echo: bool = False

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/server.db"),
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "15")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "20")),
            echo=os.getenv("DATABASE_ECHO", "false").lower() == "true",
        )


class RedisConfig(BaseModel):
    """Redis cache configuration (currently unused - placeholder for future)."""
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
    """Rate limiting configuration (implemented in core/middleware.py)."""
    enabled: bool = True
    login_max_attempts: int = 10
    login_window_secs: int = 300
    register_max_attempts: int = 5
    register_window_secs: int = 600
    webhook_max_attempts: int = 30
    webhook_window_secs: int = 60
    api_default_limit: str = "60/minute"

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        return cls(
            enabled=os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "10")),
            login_window_secs=int(os.getenv("LOGIN_WINDOW_SECS", "300")),
            register_max_attempts=int(os.getenv("REGISTER_MAX_ATTEMPTS", "5")),
            register_window_secs=int(os.getenv("REGISTER_WINDOW_SECS", "600")),
            webhook_max_attempts=int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "30")),
            webhook_window_secs=int(os.getenv("WEBHOOK_WINDOW_SECS", "60")),
            api_default_limit=os.getenv("API_DEFAULT_LIMIT", "60/minute"),
        )


class Settings(BaseModel):
    """Application settings - loaded entirely from environment variables."""
    app_name: str = "QuantPilot AI"
    app_version: str = "4.5.4"
    debug: bool = False
    json_logs: bool = False

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    cookie_secure: str = "auto"

    app_encryption_key: str = ""

    default_admin_username: str = "admin"
    default_admin_email: str = "admin@localhost"
    default_admin_password: str = ""

    position_monitor_interval_secs: int = 60
    notification_language: str = "en"

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

    def _validate_settings(self):
        """Validate settings for all environments."""
        warnings = []
        errors = []

        WEAK_PASSWORDS = {"123456", "password", "admin", "changeme", "change-me", "change_this"}
        WEAK_SECRETS = {
            "change-this-to-a-long-random-secret-at-least-32-characters",
            "your-jwt-secret", "your_jwt_secret", "changeme", "change-me",
            "secret", "jwt-secret", "jwt_secret", "tvss-change-this-secret",
        }

        if self.default_admin_password and self.default_admin_password.lower() in WEAK_PASSWORDS:
            warnings.append("DEFAULT_ADMIN_PASSWORD uses a weak default value. Change it before deployment!")

        if self.jwt_secret:
            if len(self.jwt_secret) < 32:
                warnings.append("JWT_SECRET should be at least 32 characters for security")
            normalized_secret = self.jwt_secret.lower().replace("-", "").replace("_", "")
            for weak in WEAK_SECRETS:
                if weak.replace("-", "").replace("_", "") in normalized_secret:
                    warnings.append("JWT_SECRET appears to use a placeholder value. Change it!")
                    break

        if self.server.webhook_secret:
            if len(self.server.webhook_secret) < 16:
                warnings.append("WEBHOOK_SECRET should be at least 16 characters")

        if self.server.public_base_url and "your-domain" in self.server.public_base_url.lower():
            warnings.append("PUBLIC_BASE_URL appears to use a placeholder value")

        if self.server.cors_origins == ["*"] and self.is_production:
            errors.append("CORS_ORIGINS=['*'] is not allowed in production (LIVE_TRADING=true). Set explicit origins or disable live trading.")

        if self.server.trusted_hosts == ["*"] and self.is_production:
            warnings.append("TRUSTED_HOSTS=['*'] is too permissive for production")

        if self.is_production:
            if not self.jwt_secret:
                errors.append("JWT_SECRET must be set when LIVE_TRADING=true")
            if not self.exchange.api_key or not self.exchange.api_secret:
                errors.append("Exchange API credentials required for live trading")
            if self.default_admin_password and self.default_admin_password.lower() in WEAK_PASSWORDS:
                errors.append("DEFAULT_ADMIN_PASSWORD must be changed for live trading")

        for warning in warnings:
            import warnings as warn_module
            warn_module.warn(warning, UserWarning, stacklevel=2)

        if errors:
            raise RuntimeError("\n".join(errors))

    def _validate_production_settings(self):
        """Legacy method - now calls _validate_settings."""
        self._validate_settings()

    @classmethod
    def from_env(cls) -> "Settings":
        """Create Settings instance from environment variables."""
        instance = cls(
            app_name=os.getenv("APP_NAME", "QuantPilot AI"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            json_logs=os.getenv("JSON_LOGS", "false").lower() == "true",
            jwt_secret=os.getenv("JWT_SECRET", ""),
            cookie_secure=os.getenv("COOKIE_SECURE", "auto"),
            app_encryption_key=os.getenv("APP_ENCRYPTION_KEY", ""),
            default_admin_username=os.getenv("DEFAULT_ADMIN_USERNAME", "admin"),
            default_admin_email=os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost"),
            default_admin_password=os.getenv("DEFAULT_ADMIN_PASSWORD", "").strip(),
            position_monitor_interval_secs=int(os.getenv("POSITION_MONITOR_INTERVAL_SECS", "60")),
            notification_language=os.getenv("NOTIFICATION_LANGUAGE", "en"),
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

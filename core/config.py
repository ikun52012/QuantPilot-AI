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
    # SMC cache TTL: high volatility = shorter, low = longer, normal = base
    smc_cache_ttl_enabled: bool = True
    smc_cache_ttl_base: int = 120
    smc_cache_ttl_high_vol: int = 60
    smc_cache_ttl_low_vol: int = 180
    # Pre-filter enhanced checks global timeout (seconds)
    prefilter_enhanced_timeout_secs: float = 30.0
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
    # Margin mode: cross (全仓) or isolated (逐仓)
    margin_mode: str = "cross"
    # Production safety mode: live trading should stop when required market/risk data is unavailable.
    live_data_quality_mode: str = "fail_closed"
    max_live_missing_data_checks: int = 0
    block_live_on_risk_check_error: bool = True

    @field_validator('margin_mode')
    @classmethod
    def validate_margin_mode(cls, v: str) -> str:
        normalized = str(v or "cross").lower().strip()
        if normalized not in ('cross', 'isolated'):
            raise ValueError("margin_mode must be 'cross' or 'isolated'")
        return normalized

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
            margin_mode=os.getenv("MARGIN_MODE", "cross"),
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
    breakeven_buffer_pct: float = 0.2
    step_buffer_pct: float = 0.3

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {'none', 'auto', 'moving', 'breakeven_on_tp1', 'step_trailing', 'profit_pct_trailing'}
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
    webhook_hmac_header_enabled: bool = False
    webhook_hmac_secret: str = ""
    webhook_hmac_header_name: str = "X-Webhook-Signature"
    public_base_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:8000"]
    trusted_hosts: list[str] = ["*"]
    trust_proxy_headers: bool = False

    @classmethod
    def from_env(cls) -> "ServerConfig":
        cors_raw = os.getenv("CORS_ORIGINS", "")
        cors_origins = [s.strip() for s in cors_raw.split(",") if s.strip()] if cors_raw else ["http://localhost:8000"]
        trusted_raw = os.getenv("TRUSTED_HOSTS", "")
        trusted_hosts = [s.strip() for s in trusted_raw.split(",") if s.strip()] if trusted_raw else ["*"]
        return cls(
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            webhook_hmac_header_enabled=os.getenv("WEBHOOK_HMAC_HEADER_ENABLED", "false").lower() == "true",
            webhook_hmac_secret=os.getenv("WEBHOOK_HMAC_SECRET", ""),
            webhook_hmac_header_name=os.getenv("WEBHOOK_HMAC_HEADER_NAME", "X-Webhook-Signature"),
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


class ScannerConfig(BaseModel):
    """Automatic market scanner configuration."""
    enabled: bool = False
    mode: str = "observe"
    interval_secs: int = 600
    watchlist: list[str] = Field(default_factory=list)
    source_mode: str = "manual"
    source_exchange: str = ""
    source_market_type: str = ""
    data_source_policy: str = "fallback"
    universe_top_n: int = 50
    universe_min_quote_volume: float = 5_000_000.0
    universe_cache_ttl_secs: int = 300
    confirm_max_volume_deviation_pct: float = 80.0
    include_symbols: list[str] = Field(default_factory=list)
    exclude_symbols: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    min_score: float = 65.0
    max_candidates_per_run: int = 3
    symbol_cooldown_secs: int = 1800
    setup_cooldown_secs: int = 14400
    max_signals_per_day: int = 15
    max_ai_calls_per_day: int = 30
    rsi_lower: float = 35.0
    rsi_upper: float = 65.0
    min_atr_pct: float = 0.10
    max_spread_pct: float = 0.35
    live_symbol_whitelist: list[str] = Field(default_factory=list)
    shutdown_timeout_secs: int = 30
    symbol_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_concurrent_fetches: int = 4
    bundle_cache_ttl_secs: int = 45
    ai_min_confidence: float = 0.70
    rejected_symbol_cooldown_secs: int = 300
    blocked_symbol_cooldown_secs: int = 0
    mtf_confirmation_bonus: float = 6.0
    mtf_conflict_penalty: float = 10.0
    min_volume_ratio: float = 0.15
    max_candle_gap_ratio: float = 0.15
    max_price_deviation_pct: float = 2.0
    score_weights: dict[str, float] = Field(default_factory=dict)
    ema200_enabled: bool = True
    htf_conflict_enabled: bool = True
    regime_filter_enabled: bool = True
    adaptive_threshold_enabled: bool = False
    adaptive_min_score_floor: float = 50.0
    adaptive_min_score_ceiling: float = 85.0
    adaptive_win_rate_target: float = 55.0
    adaptive_lookback_days: int = 7
    adaptive_adjustment_step: float = 2.5
    adaptive_cooldown_levels: int = 5
    adaptive_cooldown_base_secs: int = 300
    adaptive_cooldown_multiplier: float = 2.0
    learning_enabled: bool = True
    outcome_lookback_days: int = 30
    outcome_max_sync_positions: int = 50
    outcome_path_metrics_enabled: bool = True
    walk_forward_enabled: bool = True
    walk_forward_min_samples: int = 12
    walk_forward_validation_ratio: float = 0.30
    walk_forward_threshold_step: float = 2.5
    hard_filters_enabled: bool = True
    require_support_zone: bool = True
    require_structure_alignment: bool = True
    min_mtf_confirmations: int = 2
    min_rr_ratio: float = 1.40
    mtf_consensus_enabled: bool = True
    mtf_consensus_min_margin: float = 8.0
    mtf_consensus_htf_weight: float = 1.40
    mtf_consensus_ltf_weight: float = 0.80
    liquidity_filter_enabled: bool = True
    liquidity_order_size_usdt: float = 1000.0
    min_quote_volume_24h: float = 5_000_000.0
    min_orderbook_depth_usdt: float = 50_000.0
    max_estimated_slippage_pct: float = 0.25
    min_orderbook_imbalance_long: float = 0.60
    max_orderbook_imbalance_short: float = 1.80
    event_filter_enabled: bool = True
    funding_blackout_minutes: int = 10
    max_abs_funding_rate: float = 0.0015
    low_liquidity_utc_hours: list[int] = Field(default_factory=list)
    event_blackout_utc_windows: list[str] = Field(default_factory=list)
    portfolio_risk_enabled: bool = True
    max_same_direction_exposure: int = 3
    max_correlated_signals_per_run: int = 2
    correlation_buckets: dict[str, list[str]] = Field(default_factory=lambda: {
        "crypto_majors": ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC"],
        "metals": ["XAU", "XAG", "PAXG"],
        "oil": ["WTI", "BRENT", "USOIL", "UKOIL"],
    })
    scan_timeout_secs: int = 300


    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        normalized = str(v or "observe").lower().strip()
        if normalized not in {"observe", "paper", "live"}:
            raise ValueError("SCANNER_MODE must be one of: observe, paper, live")
        return normalized

    @field_validator("source_mode")
    @classmethod
    def validate_source_mode(cls, v: str) -> str:
        normalized = str(v or "manual").lower().strip()
        allowed = {"manual", "follow_exchange", "custom_exchange", "hybrid"}
        if normalized not in allowed:
            raise ValueError("SCANNER_SOURCE_MODE must be one of: manual, follow_exchange, custom_exchange, hybrid")
        return normalized

    @field_validator("data_source_policy")
    @classmethod
    def validate_data_source_policy(cls, v: str) -> str:
        normalized = str(v or "fallback").lower().strip()
        if normalized not in {"strict", "fallback", "confirm"}:
            raise ValueError("SCANNER_DATA_SOURCE_POLICY must be one of: strict, fallback, confirm")
        return normalized

    @field_validator("source_market_type")
    @classmethod
    def validate_source_market_type(cls, v: str) -> str:
        normalized = str(v or "").lower().strip()
        if normalized in {"future", "futures", "swap", "linear", "inverse"}:
            return "contract"
        if normalized not in {"", "spot", "contract"}:
            raise ValueError("SCANNER_SOURCE_MARKET_TYPE must be one of: spot, contract")
        return normalized

    @field_validator("source_exchange")
    @classmethod
    def validate_source_exchange(cls, v: str) -> str:
        return str(v or "").lower().strip()

    @field_validator("watchlist", "live_symbol_whitelist", "include_symbols", "exclude_symbols")
    @classmethod
    def validate_string_list(cls, v: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in v or []:
            value = str(item or "").strip()
            if not value:
                continue
            normalized.append(value.upper() if "/" not in value else value.upper())
        return normalized

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, v: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in v or []:
            value = str(item or "").strip().lower()
            if value:
                normalized.append(value)
        return normalized or ["15m", "1h", "4h"]

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        return cls(
            enabled=os.getenv("SCANNER_ENABLED", "false").lower() == "true",
            mode=os.getenv("SCANNER_MODE", "observe"),
            interval_secs=max(60, int(os.getenv("SCANNER_INTERVAL_SECS", "600"))),
            watchlist=_json_env("SCANNER_WATCHLIST", []),
            source_mode=os.getenv("SCANNER_SOURCE_MODE", "manual"),
            source_exchange=os.getenv("SCANNER_SOURCE_EXCHANGE", ""),
            source_market_type=os.getenv("SCANNER_SOURCE_MARKET_TYPE", ""),
            data_source_policy=os.getenv("SCANNER_DATA_SOURCE_POLICY", "fallback"),
            universe_top_n=max(1, int(os.getenv("SCANNER_UNIVERSE_TOP_N", "50"))),
            universe_min_quote_volume=max(0.0, float(os.getenv("SCANNER_UNIVERSE_MIN_QUOTE_VOLUME", "5000000"))),
            universe_cache_ttl_secs=max(0, int(os.getenv("SCANNER_UNIVERSE_CACHE_TTL_SECS", "300"))),
            confirm_max_volume_deviation_pct=max(0.0, float(os.getenv("SCANNER_CONFIRM_MAX_VOLUME_DEVIATION_PCT", "80"))),
            include_symbols=_json_env("SCANNER_INCLUDE_SYMBOLS", []),
            exclude_symbols=_json_env("SCANNER_EXCLUDE_SYMBOLS", []),
            timeframes=_json_env("SCANNER_TIMEFRAMES", ["15m", "1h", "4h"]),
            min_score=float(os.getenv("SCANNER_MIN_SCORE", "65")),
            max_candidates_per_run=max(1, int(os.getenv("SCANNER_MAX_CANDIDATES_PER_RUN", "3"))),
            symbol_cooldown_secs=max(0, int(os.getenv("SCANNER_SYMBOL_COOLDOWN_SECS", "1800"))),
            setup_cooldown_secs=max(60, int(os.getenv("SCANNER_SETUP_COOLDOWN_SECS", "14400"))),
            max_signals_per_day=max(0, int(os.getenv("SCANNER_MAX_SIGNALS_PER_DAY", "15"))),
            max_ai_calls_per_day=max(0, int(os.getenv("SCANNER_MAX_AI_CALLS_PER_DAY", "30"))),
            rsi_lower=float(os.getenv("SCANNER_RSI_LOWER", "35")),
            rsi_upper=float(os.getenv("SCANNER_RSI_UPPER", "65")),
            min_atr_pct=max(0.0, float(os.getenv("SCANNER_MIN_ATR_PCT", "0.10"))),
            max_spread_pct=max(0.0, float(os.getenv("SCANNER_MAX_SPREAD_PCT", "0.35"))),
            live_symbol_whitelist=_json_env("SCANNER_LIVE_SYMBOL_WHITELIST", []),
            shutdown_timeout_secs=max(1, int(os.getenv("SCANNER_SHUTDOWN_TIMEOUT_SECS", "30"))),
            symbol_map=_json_env("SCANNER_SYMBOL_MAP", {}),
            max_concurrent_fetches=max(1, int(os.getenv("SCANNER_MAX_CONCURRENT_FETCHES", "4"))),
            bundle_cache_ttl_secs=max(0, int(os.getenv("SCANNER_BUNDLE_CACHE_TTL_SECS", "45"))),
            ai_min_confidence=max(0.0, min(1.0, float(os.getenv("SCANNER_AI_MIN_CONFIDENCE", "0.70")))),
            rejected_symbol_cooldown_secs=max(0, int(os.getenv("SCANNER_REJECTED_SYMBOL_COOLDOWN_SECS", "300"))),
            blocked_symbol_cooldown_secs=max(0, int(os.getenv("SCANNER_BLOCKED_SYMBOL_COOLDOWN_SECS", "0"))),
            mtf_confirmation_bonus=max(0.0, float(os.getenv("SCANNER_MTF_CONFIRMATION_BONUS", "6"))),
            mtf_conflict_penalty=max(0.0, float(os.getenv("SCANNER_MTF_CONFLICT_PENALTY", "10"))),
            min_volume_ratio=max(0.0, float(os.getenv("SCANNER_MIN_VOLUME_RATIO", "0.15"))),
            max_candle_gap_ratio=max(0.0, min(1.0, float(os.getenv("SCANNER_MAX_CANDLE_GAP_RATIO", "0.15")))),
            max_price_deviation_pct=max(0.0, float(os.getenv("SCANNER_MAX_PRICE_DEVIATION_PCT", "2"))),
            score_weights=_json_env("SCANNER_SCORE_WEIGHTS", {}),
            ema200_enabled=os.getenv("SCANNER_EMA200_ENABLED", "true").lower() == "true",
            htf_conflict_enabled=os.getenv("SCANNER_HTF_CONFLICT_ENABLED", "true").lower() == "true",
            regime_filter_enabled=os.getenv("SCANNER_REGIME_FILTER_ENABLED", "true").lower() == "true",
            adaptive_threshold_enabled=os.getenv("SCANNER_ADAPTIVE_THRESHOLD_ENABLED", "false").lower() == "true",
            adaptive_min_score_floor=float(os.getenv("SCANNER_ADAPTIVE_MIN_SCORE_FLOOR", "50")),
            adaptive_min_score_ceiling=float(os.getenv("SCANNER_ADAPTIVE_MIN_SCORE_CEILING", "85")),
            adaptive_win_rate_target=float(os.getenv("SCANNER_ADAPTIVE_WIN_RATE_TARGET", "55")),
            adaptive_lookback_days=max(1, int(os.getenv("SCANNER_ADAPTIVE_LOOKBACK_DAYS", "7"))),
            adaptive_adjustment_step=max(0.0, float(os.getenv("SCANNER_ADAPTIVE_ADJUSTMENT_STEP", "2.5"))),
            adaptive_cooldown_levels=max(0, int(os.getenv("SCANNER_ADAPTIVE_COOLDOWN_LEVELS", "5"))),
            adaptive_cooldown_base_secs=max(0, int(os.getenv("SCANNER_ADAPTIVE_COOLDOWN_BASE_SECS", "300"))),
            adaptive_cooldown_multiplier=max(1.0, float(os.getenv("SCANNER_ADAPTIVE_COOLDOWN_MULTIPLIER", "2"))),
            learning_enabled=os.getenv("SCANNER_LEARNING_ENABLED", "true").lower() == "true",
            outcome_lookback_days=max(1, int(os.getenv("SCANNER_OUTCOME_LOOKBACK_DAYS", "30"))),
            outcome_max_sync_positions=max(1, int(os.getenv("SCANNER_OUTCOME_MAX_SYNC_POSITIONS", "50"))),
            outcome_path_metrics_enabled=os.getenv("SCANNER_OUTCOME_PATH_METRICS_ENABLED", "true").lower() == "true",
            walk_forward_enabled=os.getenv("SCANNER_WALK_FORWARD_ENABLED", "true").lower() == "true",
            walk_forward_min_samples=max(3, int(os.getenv("SCANNER_WALK_FORWARD_MIN_SAMPLES", "12"))),
            walk_forward_validation_ratio=max(0.1, min(0.5, float(os.getenv("SCANNER_WALK_FORWARD_VALIDATION_RATIO", "0.30")))),
            walk_forward_threshold_step=max(0.5, float(os.getenv("SCANNER_WALK_FORWARD_THRESHOLD_STEP", "2.5"))),
            hard_filters_enabled=os.getenv("SCANNER_HARD_FILTERS_ENABLED", "true").lower() == "true",
            require_support_zone=os.getenv("SCANNER_REQUIRE_SUPPORT_ZONE", "true").lower() == "true",
            require_structure_alignment=os.getenv("SCANNER_REQUIRE_STRUCTURE_ALIGNMENT", "true").lower() == "true",
            min_mtf_confirmations=max(1, int(os.getenv("SCANNER_MIN_MTF_CONFIRMATIONS", "2"))),
            min_rr_ratio=max(0.0, float(os.getenv("SCANNER_MIN_RR_RATIO", "1.40"))),
            mtf_consensus_enabled=os.getenv("SCANNER_MTF_CONSENSUS_ENABLED", "true").lower() == "true",
            mtf_consensus_min_margin=max(0.0, float(os.getenv("SCANNER_MTF_CONSENSUS_MIN_MARGIN", "8"))),
            mtf_consensus_htf_weight=max(0.1, float(os.getenv("SCANNER_MTF_CONSENSUS_HTF_WEIGHT", "1.40"))),
            mtf_consensus_ltf_weight=max(0.1, float(os.getenv("SCANNER_MTF_CONSENSUS_LTF_WEIGHT", "0.80"))),
            liquidity_filter_enabled=os.getenv("SCANNER_LIQUIDITY_FILTER_ENABLED", "true").lower() == "true",
            liquidity_order_size_usdt=max(0.0, float(os.getenv("SCANNER_LIQUIDITY_ORDER_SIZE_USDT", "1000"))),
            min_quote_volume_24h=max(0.0, float(os.getenv("SCANNER_MIN_QUOTE_VOLUME_24H", "5000000"))),
            min_orderbook_depth_usdt=max(0.0, float(os.getenv("SCANNER_MIN_ORDERBOOK_DEPTH_USDT", "50000"))),
            max_estimated_slippage_pct=max(0.0, float(os.getenv("SCANNER_MAX_ESTIMATED_SLIPPAGE_PCT", "0.25"))),
            min_orderbook_imbalance_long=max(0.0, float(os.getenv("SCANNER_MIN_ORDERBOOK_IMBALANCE_LONG", "0.60"))),
            max_orderbook_imbalance_short=max(0.0, float(os.getenv("SCANNER_MAX_ORDERBOOK_IMBALANCE_SHORT", "1.80"))),
            event_filter_enabled=os.getenv("SCANNER_EVENT_FILTER_ENABLED", "true").lower() == "true",
            funding_blackout_minutes=max(0, int(os.getenv("SCANNER_FUNDING_BLACKOUT_MINUTES", "10"))),
            max_abs_funding_rate=max(0.0, float(os.getenv("SCANNER_MAX_ABS_FUNDING_RATE", "0.0015"))),
            low_liquidity_utc_hours=[
                int(item) for item in _json_env("SCANNER_LOW_LIQUIDITY_UTC_HOURS", [])
                if str(item).isdigit() and 0 <= int(item) <= 23
            ],
            event_blackout_utc_windows=_json_env("SCANNER_EVENT_BLACKOUT_UTC_WINDOWS", []),
            portfolio_risk_enabled=os.getenv("SCANNER_PORTFOLIO_RISK_ENABLED", "true").lower() == "true",
            max_same_direction_exposure=max(1, int(os.getenv("SCANNER_MAX_SAME_DIRECTION_EXPOSURE", "3"))),
            max_correlated_signals_per_run=max(1, int(os.getenv("SCANNER_MAX_CORRELATED_SIGNALS_PER_RUN", "2"))),
            correlation_buckets=_json_env("SCANNER_CORRELATION_BUCKETS", {
                "crypto_majors": ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC"],
                "metals": ["XAU", "XAG", "PAXG"],
                "oil": ["WTI", "BRENT", "USOIL", "UKOIL"],
            }),
        )



class Settings(BaseModel):
    """Application settings - loaded entirely from environment variables."""
    app_name: str = "QuantPilot AI"
    app_version: str = "5.5.0"
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
    scanner: ScannerConfig = None  # type: ignore[assignment]

    model_config = {"arbitrary_types_allowed": True}

    @property
    def is_production(self) -> bool:
        return self.exchange.live_trading

    def _validate_settings(self):
        """Validate settings for all environments.

        P0-FIX: Enhanced validation for production safety.
        - CORS=['*'] blocked in production
        - Weak secrets/ passwords blocked
        - Database URL validation for production
        - Encryption key validation
        """
        warnings = []
        errors = []

        WEAK_PASSWORDS = {"123456", "password", "admin", "changeme", "change-me", "change_this"}
        WEAK_SECRETS = {
            "change-this-to-a-long-random-secret-at-least-32-characters",
            "your-jwt-secret", "your_jwt_secret", "changeme", "change-me",
            "secret", "jwt-secret", "jwt_secret", "tvss-change-this-secret",
        }

        if not self.default_admin_password:
            warnings.append("DEFAULT_ADMIN_PASSWORD is empty — a random password will be generated on first boot. Set a strong password in your .env file.")
        elif self.default_admin_password.lower() in WEAK_PASSWORDS:
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

        # P0-FIX: Block CORS=['*'] in production
        if self.server.cors_origins == ["*"] and self.is_production:
            errors.append("CORS_ORIGINS=['*'] is not allowed in production (LIVE_TRADING=true). Set explicit origins or disable live trading.")

        if self.server.trusted_hosts == ["*"] and self.is_production:
            errors.append("TRUSTED_HOSTS=['*'] is not allowed in production (LIVE_TRADING=true). Set explicit trusted hosts.")

        if self.debug and self.is_production:
            errors.append("DEBUG=true is not allowed in production (LIVE_TRADING=true). Disable debug mode for production safety.")

        # P0-FIX: Additional production validations
        if self.is_production:
            if not self.jwt_secret:
                errors.append("JWT_SECRET must be set when LIVE_TRADING=true")
            if not self.exchange.api_key or not self.exchange.api_secret:
                errors.append("Exchange API credentials required for live trading")
            if self.default_admin_password and self.default_admin_password.lower() in WEAK_PASSWORDS:
                errors.append("DEFAULT_ADMIN_PASSWORD must be changed for live trading")

            # P0-FIX: Validate app encryption key for production
            if not self.app_encryption_key:
                errors.append("APP_ENCRYPTION_KEY must be set for live trading (user settings encryption)")
            elif len(self.app_encryption_key) < 32:
                errors.append("APP_ENCRYPTION_KEY should be at least 32 characters (Fernet key requirement)")

            # P0-FIX: Validate JWT expiry hours
            if self.jwt_expiry_hours <= 0 or self.jwt_expiry_hours > 168:
                errors.append("JWT_EXPIRY_HOURS must be between 1 and 168 hours (1 week max)")

            # P0-FIX: Validate database URL for production
            if "sqlite" in self.database.url.lower():
                errors.append("SQLite is not recommended for production live trading. Use PostgreSQL or MySQL.")

            # P0-FIX: Validate risk settings
            if self.risk.max_position_pct > 50.0:
                warnings.append("MAX_POSITION_PCT > 50% is very risky for live trading")
            if self.risk.max_daily_loss_pct > 20.0:
                errors.append("MAX_DAILY_LOSS_PCT > 20% is too high for production")

        for warning in warnings:
            import warnings as warn_module
            warn_module.warn(warning, UserWarning, stacklevel=2)

        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {err}" for err in errors)
            raise RuntimeError(error_msg)

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
            scanner=ScannerConfig.from_env(),
        )
        instance._validate_production_settings()
        return instance


settings = Settings.from_env()

"""
Signal Server - Configuration
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel

# Load .env from project root
ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)


class AIConfig(BaseModel):
    provider: str = os.getenv("AI_PROVIDER", "deepseek")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    # Custom AI provider parameters
    custom_provider_enabled: bool = os.getenv("CUSTOM_AI_PROVIDER_ENABLED", "false").lower() == "true"
    custom_provider_name: str = os.getenv("CUSTOM_AI_PROVIDER_NAME", "custom")
    custom_provider_api_key: str = os.getenv("CUSTOM_AI_API_KEY", "")
    custom_provider_model: str = os.getenv("CUSTOM_AI_MODEL", "")
    custom_provider_api_url: str = os.getenv("CUSTOM_AI_API_URL", "")
    # Custom AI parameters
    temperature: float = float(os.getenv("AI_TEMPERATURE", "0.3"))
    max_tokens: int = int(os.getenv("AI_MAX_TOKENS", "1000"))
    custom_system_prompt: str = os.getenv("AI_CUSTOM_PROMPT", "")


class ExchangeConfig(BaseModel):
    name: str = os.getenv("EXCHANGE", "binance")
    api_key: str = os.getenv("EXCHANGE_API_KEY", "") or os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("EXCHANGE_API_SECRET", "") or os.getenv("BINANCE_API_SECRET", "")
    password: str = os.getenv("EXCHANGE_PASSWORD", "")  # OKX/Bitget passphrase
    live_trading: bool = os.getenv("LIVE_TRADING", "false").lower() == "true"


class TelegramConfig(BaseModel):
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


class RiskConfig(BaseModel):
    account_equity_usdt: float = float(os.getenv("ACCOUNT_EQUITY_USDT", "10000"))
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "10.0"))
    max_daily_trades: int = int(os.getenv("MAX_DAILY_TRADES", "10"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
    exit_management_mode: str = os.getenv("EXIT_MANAGEMENT_MODE", "ai")  # ai / custom
    custom_stop_loss_pct: float = float(os.getenv("CUSTOM_STOP_LOSS_PCT", "1.5"))
    ai_exit_system_prompt: str = os.getenv(
        "AI_EXIT_SYSTEM_PROMPT",
        "Generate a protective stop loss and up to four take-profit levels. "
        "Keep risk/reward at least 1:1.5 unless market conditions are poor.",
    )


class TakeProfitSettings(BaseModel):
    """Runtime take-profit configuration."""
    num_levels: int = int(os.getenv("TP_LEVELS", "1"))         # 1-4
    tp1_pct: float = float(os.getenv("TP1_PCT", "2.0"))       # TP1 distance %
    tp2_pct: float = float(os.getenv("TP2_PCT", "4.0"))
    tp3_pct: float = float(os.getenv("TP3_PCT", "6.0"))
    tp4_pct: float = float(os.getenv("TP4_PCT", "10.0"))
    tp1_qty: float = float(os.getenv("TP1_QTY", "25.0"))      # Close % at each TP
    tp2_qty: float = float(os.getenv("TP2_QTY", "25.0"))
    tp3_qty: float = float(os.getenv("TP3_QTY", "25.0"))
    tp4_qty: float = float(os.getenv("TP4_QTY", "25.0"))


class TrailingStopSettings(BaseModel):
    """Runtime trailing-stop configuration."""
    mode: str = os.getenv("TRAILING_STOP_MODE", "none")
    trail_pct: float = float(os.getenv("TRAILING_STOP_PCT", "1.0"))
    activation_profit_pct: float = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))
    trailing_step_pct: float = float(os.getenv("TRAILING_STEP_PCT", "0.5"))


class ServerConfig(BaseModel):
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))


class Settings(BaseModel):
    ai: AIConfig = AIConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    telegram: TelegramConfig = TelegramConfig()
    risk: RiskConfig = RiskConfig()
    take_profit: TakeProfitSettings = TakeProfitSettings()
    trailing_stop: TrailingStopSettings = TrailingStopSettings()
    server: ServerConfig = ServerConfig()


settings = Settings()

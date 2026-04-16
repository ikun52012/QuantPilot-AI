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
    provider: str = os.getenv("AI_PROVIDER", "openai")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


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
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "10.0"))
    max_daily_trades: int = int(os.getenv("MAX_DAILY_TRADES", "10"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))


class ServerConfig(BaseModel):
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))


class Settings(BaseModel):
    ai: AIConfig = AIConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    telegram: TelegramConfig = TelegramConfig()
    risk: RiskConfig = RiskConfig()
    server: ServerConfig = ServerConfig()


settings = Settings()

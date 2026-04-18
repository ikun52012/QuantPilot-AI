# 📡 Tradingview Signal Server v3.0

AI-powered cryptocurrency trading signal server with multi-exchange support, intelligent analysis, and advanced risk management.

## ✨ Features

### 🤖 Custom AI Analysis
- **3 Providers**: OpenAI (GPT), Anthropic (Claude), DeepSeek
- **Custom Parameters**: Configurable temperature, max tokens, custom system prompt
- **Intelligent Decisions**: AI analyzes signals and recommends execute/modify/reject

### 🎯 Multi Take-Profit (TP1-TP4)
- Up to **4 progressive take-profit levels**
- Independent position-close percentages per level
- AI-suggested TP prices with fallback to configured distances
- Scale out of winning trades systematically

### 📈 Advanced Trailing Stop
- **Moving Trailing**: Classic stop-loss that follows price
- **Breakeven on TP1**: Move SL to entry when TP1 is hit
- **Step Trailing**: SL moves to TP(n-1) when TP(n) is reached
- **Profit % Trailing**: Activate trailing after X% profit threshold
- **Static SL**: Traditional fixed stop-loss

### 🏦 6+ Exchange Support
Binance · OKX · Bybit · Bitget · Gate.io · Coinbase

### 📊 Live Analytics Dashboard
- Equity curves, win/loss distribution
- Sharpe, Sortino, Calmar, Profit Factor
- AI performance metrics

### Admin, Registration, and Payment Controls
- `/dashboard` redirects anonymous visitors to `/login`
- Admin panel can edit users, roles, active status, USDT balance, and subscriptions
- USDT receiving addresses can be configured for TRC20 / ERC20 / BEP20 / SOL
- Card codes can redeem balance, subscriptions, or both
- Optional invite-code-only registration with generated invite codes
- Risk exits can use AI-generated stop loss / take profits or fixed custom stop loss

## 🚀 Quick Start

```bash
# 1. Clone
git clone https://github.com/ikun52012/signal-server.git
cd signal-server

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your API keys
# First deployment admin is admin / 123456 by default.
# Set JWT_SECRET, WEBHOOK_SECRET, and change DEFAULT_ADMIN_PASSWORD before exposing the server.

# 4. Run
python main.py
```

Open `http://localhost:8000` for the homepage, or `/dashboard` for the full dashboard.

## 🐳 Docker

```bash
docker-compose up -d
```

## 📡 Signal Pipeline

```
TradingView → Webhook → Pre-Filter → AI Analysis → Decision → Exchange → Telegram
```

## ⚙️ Configuration

All settings can be configured via:
1. `.env` file (permanent)
2. Dashboard UI (runtime, persisted to `runtime_settings.json`)

### Key Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_PROVIDER` | openai / anthropic / deepseek | openai |
| `EXCHANGE` | binance / okx / bybit / bitget / gate / coinbase | binance |
| `LIVE_TRADING` | Enable real trading | false |
| `TP_LEVELS` | Number of TP levels (1-4) | 1 |
| `TRAILING_STOP_MODE` | none / moving / breakeven_on_tp1 / step_trailing / profit_pct_trailing | none |
| `TRAILING_STOP_PCT` | Trail distance % | 1.0 |
| `EXIT_MANAGEMENT_MODE` | ai / custom stop-loss and take-profit source | ai |
| `CUSTOM_STOP_LOSS_PCT` | Fixed stop loss when custom mode is selected | 1.5 |
| `AI_EXIT_SYSTEM_PROMPT` | Extra instruction for AI-generated stop loss / take profits | see `.env.example` |
| `PAYMENT_ADDRESS_TRC20` | USDT TRC20 receiving address | empty |
| `PAYMENT_ADDRESS_ERC20` | USDT ERC20 receiving address | empty |
| `PAYMENT_ADDRESS_BEP20` | USDT BEP20 receiving address | empty |
| `PAYMENT_ADDRESS_SOL` | USDT SPL receiving address | empty |

## 📝 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Homepage |
| GET | `/dashboard` | Dashboard UI |
| GET | `/api/status` | Server status |
| POST | `/webhook` | TradingView webhook |
| GET | `/api/positions` | Open positions |
| GET | `/api/history` | Trade history |
| GET | `/api/performance` | Performance analytics |
| POST | `/api/settings/take-profit` | Configure TP levels |
| POST | `/api/settings/trailing-stop` | Configure trailing stop |
| POST | `/api/settings/ai` | Configure AI provider |
| POST | `/api/settings/risk` | Configure risk and exit-generation mode |
| GET/POST | `/api/admin/registration` | Configure invite-only registration |
| GET/POST | `/api/admin/invite-codes` | List and generate invite codes |
| GET/POST | `/api/admin/redeem-codes` | List and generate card codes |
| GET/POST | `/api/admin/payment-addresses` | Configure USDT payment addresses |

## 📄 License

MIT

# TradingView Signal Server v4.1

AI-powered crypto signal server for TradingView webhooks. It includes exchange execution, AI analysis, subscription/payment controls, an admin dashboard, invite-only registration, and configurable risk exits.

## Current Highlights

- First deployment admin account: `admin / 123456`.
- Admin user management: edit username, email, role, active status, USDT balance, and subscriptions.
- Subscription system with free plans, paid plans, balance payment, USDT manual payment, and card-code redemption.
- USDT receiving address settings for TRC20, ERC20, BEP20, and SOL.
- Invite-only registration can be switched on/off from the admin panel.
- Card codes can redeem account balance, subscription access, or both.
- Risk exits can be AI-generated or custom fixed stop-loss based.
- Multi take-profit levels, trailing stop modes, analytics, Telegram notifications, and multi-exchange support.

## Quick Start

```bash
git clone https://github.com/ikun52012/signal-server.git
cd signal-server

pip install -r requirements.txt

cp .env.example .env
# Edit .env before public deployment.

python main.py
```

Open:

- Homepage: `http://localhost:8000/`
- Login: `http://localhost:8000/login`
- Dashboard: `http://localhost:8000/dashboard`

Default first-deployment login:

```text
Username: admin
Password: 123456
```

Change `DEFAULT_ADMIN_PASSWORD`, `JWT_SECRET`, and `WEBHOOK_SECRET` before exposing the service to the internet.

## Docker

```bash
docker-compose up -d
```

The SQLite database is stored under `data/server.db`. Keep this directory persistent in production so users, subscriptions, payments, invite codes, and card codes survive restarts.

## Signal Pipeline

```text
TradingView Alert
  -> /webhook
  -> pre-filter checks
  -> AI analysis
  -> risk/exit builder
  -> exchange execution or paper simulation
  -> trade log, analytics, Telegram notification
```

## Main Features

### Authentication

- `/login`, `/register`, and `/dashboard` pages are included.
- Login and registration return JWT and set an HttpOnly cookie.
- Disabled users cannot access authenticated APIs.
- Admin-only APIs re-check the user role from the database, so role changes take effect without waiting for token expiry.

### Admin Panel

Admin users can manage:

- Users: username, email, role, enabled/disabled state, and USDT balance.
- Subscriptions: grant active or pending subscriptions with optional custom duration.
- Payments: confirm or reject submitted USDT payment hashes.
- USDT receiving addresses: TRC20, ERC20, BEP20, SOL.
- Registration settings: require invite code or allow open registration.
- Invite codes: generate and copy invite codes.
- Card codes: generate balance/subscription redemption codes.

### Payments and Subscriptions

Supported payment methods:

- Free plan activation.
- Account balance payment.
- Manual USDT payment with admin confirmation.
- Card-code redemption.

Payment flow:

1. Admin configures at least one USDT address.
2. User selects a paid plan.
3. If user balance is enough, subscription activates immediately and balance is deducted.
4. Otherwise the user sends USDT to the configured address and submits TX hash.
5. Admin confirms the payment, activating the subscription.

### Registration and Invite Codes

- Public endpoint `/api/registration-settings` tells the register page whether an invite code is required.
- Admin can enable invite-only registration from the dashboard.
- Invite codes support max-use count and optional expiration date.

### Card Codes

Admin-generated card codes can grant:

- USDT balance only.
- Subscription only.
- Both balance and subscription.

Users redeem card codes from the subscription page.

### AI and Risk Management

Supported AI providers:

- DeepSeek
- OpenAI
- Anthropic
- Custom OpenAI-compatible provider

Exit management modes:

- `ai`: AI must return stop loss and take-profit targets.
- `custom`: server uses configured custom stop-loss percent and take-profit distances.

Take-profit and trailing-stop features:

- TP1 to TP4 with independent close percentages.
- Moving trailing stop.
- Breakeven on TP1.
- Step trailing.
- Profit-percent activated trailing.

## Configuration

Settings can come from:

1. `.env` for persistent deployment configuration.
2. Dashboard runtime settings, stored in `runtime_settings.json`.
3. Admin settings stored in SQLite, such as payment addresses and registration settings.

### Key Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `AI_PROVIDER` | `openai`, `anthropic`, `deepseek`, or `custom` | `deepseek` |
| `OPENAI_API_KEY` | OpenAI API key | empty |
| `ANTHROPIC_API_KEY` | Anthropic API key | empty |
| `DEEPSEEK_API_KEY` | DeepSeek API key | empty |
| `CUSTOM_AI_PROVIDER_ENABLED` | Enable custom OpenAI-compatible provider | `false` |
| `CUSTOM_AI_API_URL` | Custom provider chat-completions URL | empty |
| `EXCHANGE` | `binance`, `okx`, `bybit`, `bitget`, `gate`, `coinbase` | `binance` |
| `EXCHANGE_API_KEY` | Exchange API key | empty |
| `EXCHANGE_API_SECRET` | Exchange API secret | empty |
| `EXCHANGE_PASSWORD` | OKX/Bitget passphrase | empty |
| `LIVE_TRADING` | Enable real exchange orders | `false` |
| `WEBHOOK_SECRET` | TradingView webhook secret | required |
| `JWT_SECRET` | JWT signing secret | required for production |
| `DEFAULT_ADMIN_PASSWORD` | First admin password | `123456` |
| `ACCOUNT_EQUITY_USDT` | Equity used for risk sizing | `10000` |
| `MAX_POSITION_PCT` | Max position percent | `10.0` |
| `MAX_DAILY_TRADES` | Max daily trades | `10` |
| `MAX_DAILY_LOSS_PCT` | Max daily loss percent | `5.0` |
| `EXIT_MANAGEMENT_MODE` | `ai` or `custom` | `ai` |
| `CUSTOM_STOP_LOSS_PCT` | Fixed stop loss percent in custom mode | `1.5` |
| `AI_EXIT_SYSTEM_PROMPT` | Extra prompt for AI-generated SL/TP | see `.env.example` |
| `PAYMENT_ADDRESS_TRC20` | USDT TRC20 receiving address | empty |
| `PAYMENT_ADDRESS_ERC20` | USDT ERC20 receiving address | empty |
| `PAYMENT_ADDRESS_BEP20` | USDT BEP20 receiving address | empty |
| `PAYMENT_ADDRESS_SOL` | USDT SPL receiving address | empty |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | empty |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | empty |

## TradingView Alert Example

Use `/webhook` as the alert webhook URL.

Example JSON body:

```json
{
  "secret": "your-webhook-secret",
  "ticker": "BTCUSDT",
  "exchange": "BINANCE",
  "direction": "long",
  "price": 65000,
  "timeframe": "60",
  "strategy": "My Strategy",
  "message": "Long signal"
}
```

## API Overview

### Public Pages

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Homepage |
| GET | `/login` | Login page |
| GET | `/register` | Registration page |
| GET | `/dashboard` | Protected dashboard |

### Auth and User

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/auth/register` | Register user |
| POST | `/api/auth/login` | Login |
| POST | `/api/auth/logout` | Clear auth cookie |
| GET | `/api/auth/me` | Current user profile |
| GET | `/api/registration-settings` | Public registration settings |

### Subscription and Payment

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/plans` | Active subscription plans |
| POST | `/api/subscribe` | Subscribe to a plan |
| GET | `/api/my-subscription` | Current active subscription |
| GET | `/api/my-subscriptions` | Subscription history |
| GET | `/api/payment-options` | Configured USDT networks |
| POST | `/api/payment/create` | Create payment request |
| POST | `/api/payment/submit-tx` | Submit payment TX hash |
| GET | `/api/my-payments` | User payment history |
| POST | `/api/redeem-code` | Redeem card code |

### Admin

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/admin/users` | List users |
| PUT | `/api/admin/user/{user_id}` | Edit user profile, role, status, balance |
| POST | `/api/admin/user/{user_id}/toggle` | Enable/disable non-admin user |
| POST | `/api/admin/user/{user_id}/subscription` | Grant subscription |
| GET | `/api/admin/payments` | List payments |
| POST | `/api/admin/payment/{payment_id}/confirm` | Confirm payment |
| POST | `/api/admin/payment/{payment_id}/reject` | Reject payment |
| GET/POST | `/api/admin/plans` | List/create plans |
| PUT/DELETE | `/api/admin/plans/{plan_id}` | Update/disable plan |
| GET/POST | `/api/admin/payment-addresses` | List/set USDT addresses |
| GET/POST | `/api/admin/registration` | Get/set invite-only registration |
| GET/POST | `/api/admin/invite-codes` | List/create invite codes |
| GET/POST | `/api/admin/redeem-codes` | List/create card codes |

### Trading and Settings

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/status` | Server status and runtime config |
| GET | `/health` | Health check |
| POST | `/webhook` | TradingView webhook |
| GET | `/stats` | Today stats |
| GET | `/trades` | Today trades |
| GET | `/balance` | Exchange account balance |
| GET | `/api/positions` | Open positions |
| GET | `/api/orders` | Recent orders |
| GET | `/api/history` | Trade history |
| GET | `/api/performance` | Performance metrics |
| GET | `/api/daily-pnl` | Daily PnL |
| GET | `/api/distribution` | Trade distribution |
| POST | `/api/test-connection` | Test exchange connection |
| POST | `/api/settings/exchange` | Save exchange runtime settings |
| POST | `/api/settings/ai` | Save AI runtime settings |
| POST | `/api/settings/telegram` | Save Telegram runtime settings |
| POST | `/api/settings/risk` | Save risk/exit settings |
| POST | `/api/settings/take-profit` | Save take-profit settings |
| POST | `/api/settings/trailing-stop` | Save trailing-stop settings |
| POST | `/api/test-telegram` | Send Telegram test |
| POST | `/test-signal` | Run a manual test signal |

## Production Checklist

- Change `DEFAULT_ADMIN_PASSWORD` after first login.
- Set a strong `JWT_SECRET`.
- Set a strong `WEBHOOK_SECRET`.
- Keep `LIVE_TRADING=false` until exchange keys and risk settings are verified.
- Configure USDT receiving addresses from Admin or `.env`.
- Enable invite-only registration if public registration should be restricted.
- Back up `data/server.db`.
- Use HTTPS and set `COOKIE_SECURE=true` when deployed behind TLS.

## Local Verification

```bash
python -m compileall -f .
node --check static/app.js
git diff --check
```

## License

MIT

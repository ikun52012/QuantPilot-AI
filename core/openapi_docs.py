"""OpenAPI Documentation Enhancement."""
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def custom_openapi(app: FastAPI):
    """Generate enhanced OpenAPI documentation."""

    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="QuantPilot AI Trading API",
        version="4.3.0",
        description="""
## QuantPilot AI - Production-grade Crypto Trading Platform

### Features
- **AI Analysis**: Multi-model voting with OpenAI, Anthropic, DeepSeek
- **Backtest Engine**: 3 strategies with 25+ performance metrics
- **DCA Strategy**: Automated position averaging
- **Grid Trading**: Profit from price oscillation
- **WebSocket**: Real-time streaming for positions/prices
- **19 Pre-Filters**: Whale activity, volatility, spread checks

### Authentication
All endpoints require JWT token in cookie or Authorization header:
```
Authorization: Bearer <token>
```

### WebSocket Endpoints
Connect with JWT token as query parameter:
```
ws://localhost:8000/ws/positions?token=<jwt>
```

### Rate Limits
- Login: 5 requests per minute
- Webhook: Processed with fingerprint deduplication
- API: 100 requests per minute per user

---

**⚠️ Risk Warning**: Automated trading involves extreme risk. Test thoroughly before live deployment.
        """,
        routes=app.routes,
    )

    openapi_schema["info"]["x-logo"] = {
        "url": "https://raw.githubusercontent.com/ikun52012/QuantPilot-AI/main/logo.png"
    }

    openapi_schema["tags"] = [
        {
            "name": "Authentication",
            "description": "User login, registration, logout"
        },
        {
            "name": "Admin",
            "description": "Admin-only endpoints for system management"
        },
        {
            "name": "User",
            "description": "User settings, positions, trades"
        },
        {
            "name": "Webhook",
            "description": "TradingView webhook receiver"
        },
        {
            "name": "Backtest",
            "description": "Strategy backtesting simulation"
        },
        {
            "name": "Strategies",
            "description": "DCA and Grid trading strategies"
        },
        {
            "name": "WebSocket",
            "description": "Real-time streaming endpoints"
        },
    ]

    openapi_schema["components"]["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT token from login"
        },
        "cookieAuth": {
            "type": "apiKey",
            "in": "cookie",
            "name": "session",
            "description": "Session cookie from login"
        }
    }

    openapi_schema["security"] = [{"bearerAuth": []}, {"cookieAuth": []}]

    openapi_schema["servers"] = [
        {"url": "http://localhost:8000", "description": "Development server"},
        {"url": "https://api.quantpilot.ai", "description": "Production server"},
    ]

    openapi_schema["components"]["schemas"]["BacktestRequest"] = {
        "type": "object",
        "required": ["ticker", "strategy"],
        "properties": {
            "ticker": {"type": "string", "example": "BTCUSDT", "description": "Trading pair"},
            "timeframe": {"type": "string", "default": "1h", "enum": ["1m", "5m", "15m", "1h", "4h", "1d"]},
            "days": {"type": "integer", "default": 30, "minimum": 7, "maximum": 365},
            "strategy": {"type": "string", "enum": ["simple_trend", "smc_trend", "ai_assistant"]},
            "initial_capital": {"type": "number", "default": 10000.0},
            "stop_loss_pct": {"type": "number", "default": 2.0},
            "trailing_mode": {"type": "string", "enum": ["none", "moving", "breakeven_on_tp1", "step_trailing", "profit_pct_trailing"]},
        },
        "example": {
            "ticker": "BTCUSDT",
            "timeframe": "1h",
            "days": 30,
            "strategy": "simple_trend",
            "initial_capital": 10000,
            "stop_loss_pct": 2.0,
            "trailing_mode": "moving"
        }
    }

    openapi_schema["components"]["schemas"]["DCAConfigRequest"] = {
        "type": "object",
        "required": ["ticker", "initial_capital_usdt"],
        "properties": {
            "ticker": {"type": "string", "example": "BTCUSDT"},
            "direction": {"type": "string", "default": "long", "enum": ["long", "short"]},
            "initial_capital_usdt": {"type": "number", "default": 1000.0},
            "max_entries": {"type": "integer", "default": 5, "minimum": 2, "maximum": 10},
            "entry_spacing_pct": {"type": "number", "default": 2.0},
            "sizing_method": {"type": "string", "enum": ["fixed", "martingale", "geometric", "fibonacci"]},
            "stop_loss_pct": {"type": "number", "default": 10.0},
            "take_profit_pct": {"type": "number", "default": 5.0},
        },
        "example": {
            "ticker": "BTCUSDT",
            "direction": "long",
            "initial_capital_usdt": 1000,
            "max_entries": 5,
            "entry_spacing_pct": 2.0,
            "sizing_method": "fixed",
            "stop_loss_pct": 10.0
        }
    }

    openapi_schema["components"]["schemas"]["GridConfigRequest"] = {
        "type": "object",
        "required": ["ticker", "grid_count"],
        "properties": {
            "ticker": {"type": "string", "example": "BTCUSDT"},
            "grid_count": {"type": "integer", "default": 10, "minimum": 5, "maximum": 50},
            "total_capital_usdt": {"type": "number", "default": 1000.0},
            "grid_spacing_pct": {"type": "number", "default": 1.0},
            "spacing_mode": {"type": "string", "enum": ["arithmetic", "geometric"]},
            "mode": {"type": "string", "enum": ["neutral", "long", "short"]},
        },
        "example": {
            "ticker": "BTCUSDT",
            "grid_count": 10,
            "total_capital_usdt": 1000,
            "grid_spacing_pct": 1.0,
            "spacing_mode": "arithmetic"
        }
    }

    app.openapi_schema = openapi_schema
    return app.openapi_schema


BACKTEST_API_DESCRIPTION = """
Run a backtest simulation on historical data.

**Strategies**:
- `simple_trend`: EMA crossover strategy
- `smc_trend`: Smart Money Concepts (FVG + Order Blocks)
- `ai_assistant`: Multi-indicator (EMA + RSI + Volume)

**Returns**:
- Total trades count
- Win rate percentage
- Profit factor
- Sharpe ratio
- Max drawdown
- Equity curve
- Trade history
"""

DCA_API_DESCRIPTION = """
Create a DCA (Dollar Cost Average) strategy.

The DCA strategy automatically adds to position when price drops/rises by configured threshold.

**Modes**:
- `average_down`: Add to position when price drops
- `average_up`: Add to position when price rises

**Sizing Methods**:
- `fixed`: Same size each entry
- `martingale`: 1.5x multiplier each entry
- `geometric`: Progressive increase
- `fibonacci`: Fibonacci sequence sizing
"""

GRID_API_DESCRIPTION = """
Create a Grid Trading strategy.

Grid trading profits from price oscillation within a range by placing buy orders below current price and sell orders above.

**Modes**:
- `neutral`: Equal buy/sell distribution
- `long`: More buy levels (bullish bias)
- `short`: More sell levels (bearish bias)

**Spacing**:
- `arithmetic`: Fixed price difference between levels
- `geometric`: Percentage-based spacing
"""

WEBSOCKET_API_DESCRIPTION = """
WebSocket endpoints for real-time streaming.

**Connection**:
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/positions?token=<jwt>');
ws.onmessage = (event) => console.log(JSON.parse(event.data));
```

**Message Types**:
- `position_update`: Real-time PnL updates
- `price_update`: Live price streaming
- `system_status`: Server health (admin only)
"""
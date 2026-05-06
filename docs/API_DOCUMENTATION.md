# QuantPilot AI v4.5.5 - API Documentation

## Overview

QuantPilot AI is an intelligent cryptocurrency trading system with multi-exchange support, AI-powered analysis, and comprehensive risk management.

## API Versions

### Version Management

QuantPilot supports multiple API versions for smooth migration:

- **v1** (deprecated, sunset: 2025-08-01) - Legacy API
- **v2** (current, stable) - Modern API with enhanced features
- **Default**: v1 (for backward compatibility)

### How to Specify Version

1. **Header**: `X-API-Version: v2`
2. **URL Prefix**: `/api/v2/...`
3. **Default**: Uses v1 if not specified

---

## Endpoints

### Trading Endpoints

#### Execute Trade (v2)

```
POST /api/v2/trade/execute
```

Execute a new trade with AI analysis and multi-TP support.

**Request Body**:
```json
{
  "ticker": "BTCUSDT",
  "direction": "long",
  "signal_price": 50000.0,
  "timeframe": "1h",
  "strategy": "momentum",
  "message": "Strong bullish signal"
}
```

**Response**:
```json
{
  "version": "v2",
  "status": "success",
  "order_id": "binance_order_123",
  "symbol": "BTC/USDT:USDT",
  "direction": "long",
  "quantity": 0.01,
  "entry_price": 50000.0,
  "leverage": 10,
  "stop_loss": 48000.0,
  "take_profit_levels": [
    {"price": 51000, "qty_pct": 25},
    {"price": 52000, "qty_pct": 25},
    {"price": 53000, "qty_pct": 25},
    {"price": 54000, "qty_pct": 25}
  ],
  "ai_confidence": 0.85,
  "risk_score": 0.4,
  "timestamp": "2026-05-06T10:00:00Z"
}
```

#### Get Positions

```
GET /api/v2/positions?status=open&exchange=binance
```

List active positions with filtering.

**Query Parameters**:
- `status`: Position status (open/closed/all)
- `exchange`: Exchange name
- `ticker`: Trading pair
- `user_id`: User ID

**Response**:
```json
{
  "positions": [
    {
      "id": "pos_123",
      "ticker": "BTCUSDT",
      "direction": "long",
      "entry_price": 50000.0,
      "current_price": 51000.0,
      "quantity": 0.01,
      "leverage": 10,
      "unrealized_pnl_usdt": 10.0,
      "current_pnl_pct": 2.0,
      "status": "open",
      "opened_at": "2026-05-06T09:00:00Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

#### Close Position

```
POST /api/v2/positions/{position_id}/close
```

Manually close a position.

---

### AI Analysis Endpoints

#### Analyze Signal

```
POST /api/v2/ai/analyze
```

Get AI analysis for a trading signal.

**Request Body**:
```json
{
  "signal": {
    "ticker": "BTCUSDT",
    "direction": "long",
    "price": 50000.0
  },
  "market_context": {
    "current_price": 50000.0,
    "volume_24h": 1000000.0,
    "rsi_1h": 60.0,
    "atr_pct": 2.5
  }
}
```

**Response**:
```json
{
  "confidence": 0.85,
  "recommendation": "execute",
  "reasoning": "Strong bullish setup with good risk/reward ratio",
  "suggested_entry": 50000.0,
  "suggested_stop_loss": 48000.0,
  "suggested_take_profit_levels": [
    {"price": 51000, "qty_pct": 25},
    {"price": 52000, "qty_pct": 25}
  ],
  "recommended_leverage": 10,
  "risk_score": 0.4,
  "market_condition": "trending_up",
  "warnings": [],
  "analysis_time_ms": 1500,
  "cache_layer": "L1_memory",
  "provider": "deepseek",
  "model": "deepseek-v4-pro"
}
```

---

### Monitoring Endpoints

#### Get System Metrics

```
GET /api/v2/metrics
```

Get Prometheus metrics for observability.

**Response**:
```
# HELP quantpilot_trade_total Total trades executed
# TYPE quantpilot_trade_total counter
quantpilot_trade_total{exchange="binance",symbol="BTCUSDT",direction="long",result="success"} 123

# HELP quantpilot_position_count Active positions
# TYPE quantpilot_position_count gauge
quantpilot_position_count{exchange="binance",symbol="BTCUSDT",direction="long"} 5
```

#### Get Cache Metrics

```
GET /api/v2/cache/metrics
```

Get multi-layer cache performance metrics.

**Response**:
```json
{
  "ai_analysis_cache": {
    "l1_size": 450,
    "l1_hits": 850,
    "l1_misses": 50,
    "l2_hits": 30,
    "l3_hits": 10,
    "total_hits": 890,
    "hit_rate_pct": 89.0,
    "evictions": 5
  },
  "smc_analysis_cache": {
    "hit_rate_pct": 92.0
  }
}
```

#### Get Event Bus Metrics

```
GET /api/v2/events/metrics
```

Get event bus statistics.

**Response**:
```json
{
  "events_published": 500,
  "events_processed": 500,
  "handlers_executed": 1500,
  "handler_errors": 2,
  "handlers_registered": 12
}
```

---

## Authentication

### JWT Authentication

Include JWT token in Authorization header:

```
Authorization: Bearer <jwt_token>
```

### API Key Authentication (Alternative)

```
X-API-Key: <api_key>
X-API-Secret: <api_secret>
```

---

## Rate Limiting

Default rate limits:
- **Standard**: 60 requests/minute
- **Premium**: 300 requests/minute

Rate limit headers:
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 3600
```

---

## Error Responses

Standard error format:

```json
{
  "error": {
    "code": "TRADE_EXECUTION_FAILED",
    "message": "Trade execution failed: insufficient balance",
    "details": {
      "required_balance": 100.0,
      "available_balance": 50.0
    },
    "timestamp": "2026-05-06T10:00:00Z",
    "trace_id": "abc123"
  }
}
```

---

## WebSocket Endpoints

### Real-time Position Updates

```
WS /api/v2/ws/positions
```

Subscribe to real-time position updates.

**Message Format**:
```json
{
  "event": "position_update",
  "data": {
    "position_id": "pos_123",
    "current_pnl_pct": 2.5,
    "last_price": 51250.0
  }
}
```

---

## Deprecation Notices

### API Version v1 Deprecation

- **Deprecated**: 2025-05-01
- **Sunset**: 2025-08-01
- **Migration Guide**: `/docs/api-migration-v1-to-v2`

Headers for deprecated v1:
```
Deprecation: true; sunset=2025-08-01
Warning: 299 - "Deprecated API version v1. Will be removed on 2025-08-01. Use v2 instead."
Link: <https://docs.quantpilot.ai/api-migration>; rel="deprecation"
```

---

## SDK Examples

### Python

```python
from quantpilot_sdk import QuantPilotClient

client = QuantPilotClient(
    api_key="your_api_key",
    api_secret="your_api_secret",
    version="v2"
)

# Execute trade
result = await client.trade.execute({
    "ticker": "BTCUSDT",
    "direction": "long",
    "signal_price": 50000.0
})

# Get positions
positions = await client.positions.list(status="open")
```

### JavaScript

```javascript
const QuantPilot = require('quantpilot-sdk');

const client = new QuantPilot({
  apiKey: 'your_api_key',
  apiSecret: 'your_api_secret',
  version: 'v2'
});

// Execute trade
const result = await client.trade.execute({
  ticker: 'BTCUSDT',
  direction: 'long',
  signal_price: 50000.0
});
```

---

## Changelog

### v4.5.5 (2026-05-06)

**New Features**:
- Multi-layer intelligent cache (L1/L2/L3)
- Event-driven architecture with EventBus
- Prometheus metrics integration
- Dynamic ghost position thresholds
- Leverage setup retry mechanism

**Improvements**:
- AI cache race condition fixed (double-check locking)
- Production config validation enforced
- Database indexes optimized
- Structured JSON logging

**Bug Fixes**:
- Fixed AI cache lazy initialization race condition
- Fixed leverage setup failure handling
- Fixed ghost position threshold too aggressive
- Fixed CORS=['*'] allowed in production

---

## Support

- **Documentation**: https://docs.quantpilot.ai
- **API Status**: https://status.quantpilot.ai
- **Support Email**: support@quantpilot.ai
- **GitHub Issues**: https://github.com/quantpilot/quantpilot-ai/issues
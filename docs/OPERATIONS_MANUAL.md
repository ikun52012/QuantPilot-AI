# QuantPilot AI v4.5.5 - Operations Manual

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Deployment](#deployment)
3. [Configuration](#configuration)
4. [Monitoring](#monitoring)
5. [Troubleshooting](#troubleshooting)
6. [Maintenance](#maintenance)
7. [Security](#security)
8. [Backup & Recovery](#backup--recovery)

---

## System Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│                 QuantPilot AI Architecture               │
└─────────────────────────────────────────────────────────┘

┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  TradingView │───▶│  Signal API │───▶│  Pre-Filter │
│   Webhook    │    │  Receiver   │    │  Engine     │
└─────────────┘    └─────────────┘    └─────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────┐
│                    AI Analysis Pipeline                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐    │
│  │  Cache  │  │   AI    │  │   SMC   │  │  Vote   │    │
│  │ L1/L2/L3│  │ Provider│  │ Analyzer│  │ Engine  │    │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘    │
└─────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────┐
│              Multi-Exchange Executor                     │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐           │
│  │ Binance│ │   OKX  │ │ Bybit  │ │ Bitget │           │
│  └────────┘ └────────┘ └────────┘ └────────┘           │
└─────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────┐
│            Position Monitor & Risk Manager               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Position DB  │  │ Trailing Stop│  │  Ghost Detect │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────┐
│                 Observability Stack                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Prometheus   │  │   Grafana    │  │  Event Bus   │  │
│  │   Metrics    │  │  Dashboards  │  │   Store      │  │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Signal Reception**: TradingView webhook → Signal validation
2. **Pre-Filter**: 29 intelligent checks → Score calculation
3. **AI Analysis**: Multi-layer cache → AI provider → SMC analysis
4. **Trade Decision**: Risk assessment → Position sizing
5. **Execution**: Exchange adapter → Order placement → TP/SL setup
6. **Monitoring**: Position reconciliation → Trailing stop → Ghost detection

---

## Deployment

### Production Deployment Checklist

```bash
# 1. Environment Setup
□ PYTHON_VERSION=3.11+
□ DATABASE=PostgreSQL 14+
□ REDIS=7.0+ (optional, for L2 cache)
□ EXCHANGE_API_KEYS configured
□ AI_API_KEYS configured

# 2. Security Checks
□ JWT_SECRET length >= 32 characters
□ APP_ENCRYPTION_KEY set (Fernet key)
□ CORS_ORIGINS != ['*']
□ DEFAULT_ADMIN_PASSWORD changed
□ API keys not in default/weak values

# 3. Configuration Validation
□ LIVE_TRADING=false (test first)
□ SANDBOX_MODE=true for testing
□ EXCHANGE_SANDBOX_MODE=true
□ DATABASE_URL using PostgreSQL
□ REDIS_ENABLED=false initially

# 4. Database Setup
□ Migrations applied
□ Indexes created
□ Initial admin user created
□ Backup strategy configured

# 5. Monitoring Setup
□ Prometheus scraping enabled
□ Grafana dashboards imported
□ Alerting rules configured
□ Log aggregation enabled
```

### Docker Deployment

```yaml
# docker-compose.prod.yml
version: '3.8'

services:
  quantpilot:
    image: quantpilot:4.5.5
    container_name: quantpilot-api
    restart: always
    ports:
      - "8000:8000"
    environment:
      - ENV=production
      - DATABASE_URL=postgresql://user:pass@postgres:5432/quantpilot
      - REDIS_URL=redis://redis:6379/0
      - LIVE_TRADING=true
      - CORS_ORIGINS=https://your-domain.com
      - JWT_SECRET=${JWT_SECRET}
      - APP_ENCRYPTION_KEY=${APP_ENCRYPTION_KEY}
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./config:/app/config
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
    depends_on:
      - postgres
      - redis
    networks:
      - quantpilot-net

  postgres:
    image: postgres:14-alpine
    container_name: quantpilot-db
    restart: always
    environment:
      POSTGRES_DB: quantpilot
      POSTGRES_USER: quantpilot
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U quantpilot"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - quantpilot-net

  redis:
    image: redis:7-alpine
    container_name: quantpilot-cache
    restart: always
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - quantpilot-net

  prometheus:
    image: prom/prometheus:latest
    container_name: quantpilot-metrics
    restart: always
    ports:
      - "9090:9090"
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml
      - ./config/alerting_rules.yml:/etc/prometheus/alerting_rules.yml
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
    networks:
      - quantpilot-net

  grafana:
    image: grafana/grafana:latest
    container_name: quantpilot-dashboard
    restart: always
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./config/grafana/dashboards:/etc/grafana/provisioning/dashboards
    networks:
      - quantpilot-net

volumes:
  postgres_data:
  redis_data:
  grafana_data:

networks:
  quantpilot-net:
    driver: bridge
```

### Start Services

```bash
# Start all services
docker-compose -f docker-compose.prod.yml up -d

# Check health
docker-compose -f docker-compose.prod.yml ps

# View logs
docker-compose -f docker-compose.prod.yml logs -f quantpilot

# Scale (if needed)
docker-compose -f docker-compose.prod.yml up -d --scale quantpilot=2
```

---

## Configuration

### Environment Variables (Production)

```bash
# .env.production

# Application
APP_NAME=QuantPilot AI
APP_VERSION=4.5.5
DEBUG=false
JSON_LOGS=true

# Database (PostgreSQL required for production)
DATABASE_URL=postgresql://quantpilot:password@localhost:5432/quantpilot
DATABASE_POOL_SIZE=30
DATABASE_MAX_OVERFLOW=20

# Redis Cache (optional)
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
REDIS_TTL=300

# Security (MUST be changed for production)
JWT_SECRET=your-very-long-random-secret-at-least-32-characters-change-this
APP_ENCRYPTION_KEY=your-fernet-key-32-characters-base64-encoded
DEFAULT_ADMIN_PASSWORD=CHANGE_THIS_PASSWORD

# Server
HOST=0.0.0.0
PORT=8000
CORS_ORIGINS=https://your-domain.com,https://app.your-domain.com
TRUSTED_HOSTS=your-domain.com,api.your-domain.com

# Trading
EXCHANGE=binance
EXCHANGE_API_KEY=your_exchange_api_key
EXCHANGE_API_SECRET=your_exchange_api_secret
LIVE_TRADING=true  # Enable only after testing
EXCHANGE_SANDBOX_MODE=false  # Disable after testing
EXCHANGE_MARKET_TYPE=contract

# AI Configuration
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key
AI_TEMPERATURE=0.3
AI_MAX_TOKENS=1000
AI_READ_TIMEOUT_SECS=90

# Risk Management
ACCOUNT_EQUITY_USDT=10000
MAX_POSITION_PCT=10.0
MAX_DAILY_TRADES=10
MAX_DAILY_LOSS_PCT=5.0
MARGIN_MODE=cross

# Monitoring
POSITION_MONITOR_INTERVAL_SECS=60
TELEGRAM_BOT_TOKEN=your_telegram_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Configuration Hot-Reload

Supported hot-reload parameters:
- ✅ `trading.leverage`
- ✅ `trading.max_position_pct`
- ✅ `ai.timeout`
- ✅ `position_monitor.ghost_threshold`
- ❌ `exchange.api_key` (requires restart)
- ❌ `database.url` (requires restart)

---

## Monitoring

### Prometheus Metrics

Key metrics to monitor:

```promql
# Trade success rate
rate(quantpilot_trade_total{result="success"}[5m])
/
rate(quantpilot_trade_total[5m])

# AI cache hit rate
rate(quantpilot_ai_cache_hit_total[30m])
/
rate(quantpilot_ai_analysis_total[30m])

# Position count
sum(quantpilot_position_count)

# Error rate
rate(quantpilot_error_rate_total{severity="critical"}[5m])

# Ghost positions
quantpilot_ghost_position_count

# Database connection pool usage
quantpilot_db_connection_pool_used
/
quantpilot_db_connection_pool_size
```

### Grafana Dashboards

Import dashboards from `config/grafana/dashboards/`:
- **Trading Overview**: Trade success/failure rates, PnL
- **AI Performance**: Cache hit rates, analysis latency
- **System Health**: Error rates, database metrics
- **Position Monitor**: Active positions, ghost detections

### Log Analysis

Structured JSON logs in `logs/quantpilot_YYYY-MM-DD.json`:

```bash
# Search errors
cat logs/quantpilot_*.json | jq 'select(.level=="ERROR")'

# Search by trace_id
cat logs/quantpilot_*.json | jq 'select(.trace_id=="abc123")'

# Search by exchange
cat logs/quantpilot_*.json | jq 'select(.exchange=="binance")'

# Count errors by type
cat logs/quantpilot_*.json | jq 'select(.level=="ERROR") | .error_type' | sort | uniq -c
```

---

## Troubleshooting

### Common Issues

#### 1. Ghost Position Detected

**Symptoms**: Position shows in database but not on exchange

**Diagnosis**:
```bash
# Check position_monitor logs
grep "ghost" logs/quantpilot_*.json | jq

# Check exchange API status
curl -X GET https://api.binance.com/api/v3/ping

# Manual position sync
python scripts/sync_positions.py --exchange binance
```

**Solution**:
```bash
# Adjust threshold if too sensitive
# config/runtime.json
{
  "position_monitor": {
    "ghost_threshold_multiplier": 2.0
  }
}

# Or manually close ghost position
curl -X POST http://localhost:8000/api/v2/admin/positions/{id}/force-close
```

#### 2. AI Analysis Timeout

**Symptoms**: High timeout rate, slow trade decisions

**Diagnosis**:
```bash
# Check AI provider status
curl -I https://api.deepseek.com/health

# Check cache hit rate
curl http://localhost:8000/api/v2/cache/metrics

# Check timeout config
grep AI_READ_TIMEOUT_SECS .env
```

**Solution**:
```bash
# Reduce timeout
AI_READ_TIMEOUT_SECS=30

# Enable cache
REDIS_ENABLED=true

# Use faster model
DEEPSEEK_MODEL=deepseek-v4-flash
```

#### 3. Leverage Setup Failure

**Symptoms**: Trades aborted with "Leverage setup Failed"

**Diagnosis**:
```bash
# Check leverage retry logs
grep "leverage_setup_failure" logs/quantpilot_*.json | jq

# Check exchange leverage limits
curl -X GET "https://api.binance.com/api/v3/leverageBracket?symbol=BTCUSDT"
```

**Solution**:
```bash
# Reduce leverage request
MAX_LEVERAGE=20

# Check margin mode
MARGIN_MODE=cross  # or isolated

# Retry mechanism handles transient errors automatically
```

#### 4. Database Connection Pool Exhausted

**Symptoms**: "Too many connections" errors

**Diagnosis**:
```bash
# Check pool metrics
curl http://localhost:8000/api/v2/metrics | grep db_connection_pool

# Check PostgreSQL connections
psql -c "SELECT count(*) FROM pg_stat_activity;"
```

**Solution**:
```bash
# Increase pool size
DATABASE_POOL_SIZE=50
DATABASE_MAX_OVERFLOW=30

# Kill idle connections
psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle';"

# Optimize slow queries (add indexes)
```

---

## Maintenance

### Daily Tasks

```bash
□ Check Prometheus metrics dashboard
□ Review ghost position logs
□ Check error rate trends
□ Verify backup completed
□ Review trade success rates
□ Check disk space usage
□ Review AI cache hit rates
```

### Weekly Tasks

```bash
□ Analyze slow database queries
□ Review position_monitor performance
□ Check Redis memory usage
□ Audit security logs
□ Review configuration changes
□ Test alerting rules
□ Update Grafana dashboards
```

### Monthly Tasks

```bash
□ Security audit
□ Dependency update check
□ Database optimization (VACUUM, ANALYZE)
□ Backup restoration test
□ Performance benchmark
□ Capacity planning review
□ Documentation update
```

---

## Security

### Security Checklist

```bash
# Authentication
□ JWT_SECRET changed from default
□ JWT expiry reasonable (1-168 hours)
□ Admin password changed
□ 2FA enabled for admin accounts

# Encryption
□ APP_ENCRYPTION_KEY set
□ TLS/SSL enabled for API
□ Database connection encrypted

# Network
□ CORS_ORIGINS not ['*']
□ TRUSTED_HOSTS configured
□ Rate limiting enabled
□ Firewall rules configured

# Secrets Management
□ No secrets in code
□ Secrets in environment variables
□ API keys rotated regularly
□ Encryption keys backed up

# Audit
□ Login attempts logged
□ Configuration changes logged
□ Admin actions audited
□ Position changes tracked
```

### Incident Response

1. **Detection**: Prometheus alerts → Grafana dashboard
2. **Analysis**: Structured logs with trace_id → Event store
3. **Containment**: Disable live_trading → Pause new trades
4. **Resolution**: Fix root cause → Verify fix
5. **Recovery**: Resume trading → Monitor closely
6. **Documentation**: Update incident log → Review procedures

---

## Backup & Recovery

### Backup Strategy

```bash
# Database backup (daily)
pg_dump -U quantpilot quantpilot > backup_$(date +%Y%m%d).sql

# Redis backup (hourly)
redis-cli BGSAVE

# Configuration backup
cp -r config/ backup/config_$(date +%Y%m%d)/

# Log backup (30 days retention)
tar -czf logs_$(date +%Y%m%d).tar.gz logs/
```

### Recovery Procedure

```bash
# 1. Stop services
docker-compose down

# 2. Restore database
psql -U quantpilot quantpilot < backup_20260506.sql

# 3. Restore Redis
redis-cli --rdb backup/dump.rdb

# 4. Restore config
cp -r backup/config_20260506/ config/

# 5. Restart services
docker-compose up -d

# 6. Verify
curl http://localhost:8000/health
curl http://localhost:8000/api/v2/metrics
```

---

## Performance Tuning

### Database Optimization

```sql
-- Add missing indexes (P1-FIX)
CREATE INDEX CONCURRENTLY idx_positions_status_opened_at ON positions(status, opened_at);
CREATE INDEX CONCURRENTLY idx_positions_ticker_status ON positions(ticker, status);

-- Analyze tables
ANALYZE positions;
ANALYZE users;

-- Vacuum ( reclaim space)
VACUUM ANALYZE positions;
```

### Cache Optimization

```bash
# Increase L1 cache size
export AI_CACHE_MAX_SIZE=1000
export SMC_CACHE_MAX_SIZE=500

# Enable Redis L2 cache
export REDIS_ENABLED=true
export REDIS_URL=redis://localhost:6379/0

# Adjust TTL
export AI_CACHE_BASE_TTL=120
export SMC_CACHE_BASE_TTL=300
```

### AI Performance

```bash
# Use faster model for high load
export AI_PROVIDER=openai
export OPENAI_MODEL=gpt-4o-mini  # Faster than gpt-4

# Enable voting for critical decisions
export AI_VOTING_ENABLED=true
export AI_VOTING_MODELS='["deepseek-v4-pro","gpt-4o-mini"]'

# Increase timeout for complex analysis
export AI_READ_TIMEOUT_SECS=120
```

---

## Support Contacts

- **Technical Support**: support@quantpilot.ai
- **Emergency Hotline**: +1-XXX-XXX-XXXX
- **GitHub Issues**: https://github.com/quantpilot/quantpilot-ai/issues
- **Documentation**: https://docs.quantpilot.ai
- **Status Page**: https://status.quantpilot.ai
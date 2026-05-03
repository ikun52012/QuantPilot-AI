# QuantPilot AI — Production Deployment Checklist

## Security (Critical)

- [ ] Set a strong `JWT_SECRET` (at least 48 random characters)
- [ ] Set strong `WEBHOOK_SECRET` for webhook payload authentication
- [ ] Change `POSTGRES_PASSWORD` and `REDIS_PASSWORD` from defaults
- [ ] Change the admin password immediately after first login
- [ ] Set `COOKIE_SECURE=force` when behind HTTPS
- [ ] Set `TRUST_PROXY_HEADERS=true` only if behind a trusted reverse proxy
- [ ] Configure Nginx/Caddy with SSL (see `nginx.conf.example`)
- [ ] Verify `LIVE_TRADING=false` until fully tested

## Infrastructure

- [ ] Use PostgreSQL (not SQLite) for production: set `DATABASE_URL`
- [ ] Enable Redis: set `REDIS_ENABLED=true` and `REDIS_URL`
- [ ] Configure log rotation (Docker json-file driver handles this)
- [ ] Mount persistent volumes for `data/`, `logs/`, `trade_logs/`
- [ ] Set `PUBLIC_BASE_URL` to your domain (e.g. `https://trade.example.com`)

## AI Provider

- [ ] Set at least one AI provider API key
- [ ] Test AI analysis with paper trading before going live
- [ ] Monitor AI costs via Admin → AI Costs endpoint

## Exchange

- [ ] Use exchange API keys with minimal permissions (trade only, no withdraw)
- [ ] Test with `EXCHANGE_SANDBOX_MODE=true` first
- [ ] Set appropriate `MAX_POSITION_PCT` and `MAX_DAILY_TRADES` limits

## Monitoring

- [ ] Verify `/health` endpoint returns healthy
- [ ] Set up Telegram notifications for trade alerts
- [ ] Enable Prometheus/Grafana (uncomment in docker-compose.yml)
- [ ] Monitor disk space for database and log growth

## Backup

- [ ] Automated daily backups run at 02:00 UTC (built-in)
- [ ] Test backup restore procedure at least once
- [ ] Keep off-site backup copies of `data/` directory

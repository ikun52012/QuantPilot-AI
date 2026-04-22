# ─────────────────────────────────────────────
# Multi-stage build for smaller image
# ─────────────────────────────────────────────

# Builder stage
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ─────────────────────────────────────────────
# Production stage
# ─────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY . .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && mkdir -p data/backups logs trade_logs \
    && chown -R appuser:appgroup /app/data /app/logs /app/trade_logs \
    && chmod -R 775 /app/data /app/logs /app/trade_logs

RUN cp -r /root/.local /home/appuser/.local 2>/dev/null || true \
    && chown -R appuser:appgroup /home/appuser/.local 2>/dev/null || true

USER appuser

ENV PATH=/home/appuser/.local/bin:/root/.local/bin:$PATH

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

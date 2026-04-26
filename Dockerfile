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
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────
# Production stage
# ─────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Create appuser with fixed uid 1000 to match typical host user
# This avoids permission issues when mounting host volumes (./data, ./logs, etc.)
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -m -d /home/appuser -s /bin/bash appuser

COPY --chown=appuser:appgroup . .

RUN mkdir -p data/backups logs trade_logs \
    && chmod -R 775 data logs trade_logs \
    && chmod +x scripts/docker-entrypoint.sh

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["scripts/docker-entrypoint.sh"]

#!/bin/sh
set -e

echo "[Entrypoint] Starting QuantPilot AI..."

# Run database migrations if Alembic is configured
if [ -f "alembic.ini" ] && [ "${SKIP_MIGRATIONS}" != "true" ]; then
    echo "[Entrypoint] Running database migrations..."
    alembic upgrade head || echo "[Entrypoint] Alembic upgrade failed; continuing with existing schema"
fi

# Start the application
echo "[Entrypoint] Launching application..."
exec uvicorn app:app --host 0.0.0.0 --port 8000

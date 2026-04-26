#!/bin/sh
set -e

echo "[Entrypoint] Starting QuantPilot AI..."

# Run database migrations if Alembic is configured
if [ -f "alembic.ini" ] && [ "${SKIP_MIGRATIONS}" != "true" ]; then
    echo "[Entrypoint] Running database migrations..."
    # Use Python to run migrations since we might not have alembic installed directly
    python -c "
import asyncio
from core.database import db_manager
asyncio.run(db_manager.init())
print('[Entrypoint] Database schema initialized')
" || echo "[Entrypoint] Database initialization skipped (may already be initialized)"
fi

# Start the application
echo "[Entrypoint] Launching application..."
exec uvicorn app:app --host 0.0.0.0 --port 8000
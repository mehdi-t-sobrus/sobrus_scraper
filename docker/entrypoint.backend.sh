#!/bin/bash
# docker/entrypoint.backend.sh
set -e

echo "=== Django Backend ==="

# Parse host and port from DATABASE_URL
# Format: postgresql://user:pass@host:port/db
DB_URL="${DATABASE_URL:-}"
DB_HOST=$(echo "$DB_URL" | sed -E 's|.*@([^:/]+).*|\1|')
DB_PORT=$(echo "$DB_URL" | sed -E 's|.*:([0-9]+)/.*|\1|')
DB_PORT="${DB_PORT:-5432}"

echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."

until nc -z "${DB_HOST}" "${DB_PORT}" 2>/dev/null; do
    echo "  PostgreSQL not ready — retrying in 2s..."
    sleep 2
done
echo "  PostgreSQL port open."

# Additional wait for PostgreSQL to fully accept connections
sleep 2

echo "Running migrations..."
python src/backend/manage.py migrate --no-input

echo "Collecting static files..."
python src/backend/manage.py collectstatic --no-input --clear 2>/dev/null || true

echo "Starting Gunicorn..."
exec gunicorn core.asgi:application \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${GUNICORN_WORKERS:-4}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout 120 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --chdir /app/src/backend

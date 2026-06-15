#!/bin/bash
# docker/entrypoint.dagster_web.sh
set -e

echo "=== Dagster Webserver ==="

# Parse host and port from DATABASE_URL
DB_URL="${DATABASE_URL:-}"
DB_HOST=$(echo "$DB_URL" | sed -E 's|.*@([^:/]+).*|\1|')
DB_PORT=$(echo "$DB_URL" | sed -E 's|.*:([0-9]+)/.*|\1|')
DB_PORT="${DB_PORT:-5432}"

echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."

until nc -z "${DB_HOST}" "${DB_PORT}" 2>/dev/null; do
    echo "  PostgreSQL not ready — retrying in 2s..."
    sleep 2
done
echo "  PostgreSQL ready."

echo "Starting Dagster webserver..."
exec dagster-webserver \
    -f /app/src/orchestration/definitions.py \
    -h 0.0.0.0 \
    -p 3000

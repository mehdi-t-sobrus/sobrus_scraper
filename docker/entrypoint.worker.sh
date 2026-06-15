#!/bin/bash
# docker/entrypoint.worker.sh
set -e

echo "=== Arq Scraping Worker ==="

# Wait for Redis
until python -c "
import redis, os, sys
try:
    r = redis.from_url(os.environ.get('REDIS_URL', 'redis://redis:6379/0'))
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    echo "  Redis not ready — retrying in 2s..."
    sleep 2
done
echo "  Redis ready."

# Wait for Django backend to be healthy (migrations must run first)
until curl -sf "http://backend:8000/api/v1/health/" > /dev/null 2>&1; do
    echo "  Backend not ready — retrying in 3s..."
    sleep 3
done
echo "  Backend ready."

echo "Starting Arq worker..."
exec python -m arq scrapers.worker.WorkerSettings

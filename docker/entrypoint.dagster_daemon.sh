#!/bin/bash
# docker/entrypoint.dagster_daemon.sh
set -e

echo "=== Dagster Daemon ==="

# Wait for webserver
echo "Waiting for Dagster webserver..."
until curl -sf "http://dagster_web:3000/server_info" > /dev/null 2>&1; do
    echo "  Dagster webserver not ready — retrying in 3s..."
    sleep 3
done
echo "  Dagster webserver ready."

echo "Starting Dagster daemon (schedules + sensors)..."
exec dagster-daemon run \
    -f /app/src/orchestration/definitions.py

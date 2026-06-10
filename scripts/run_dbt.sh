#!/bin/bash
# =============================================================================
# scripts/run_dbt.sh
# Run dbt Silver layer with env vars loaded from src/transformations/.env
# Must be run from the repo root.
#
# USAGE
# -----
# Always provide explicit dates to avoid reprocessing all Bronze files:
#
#   ./scripts/run_dbt.sh run --select silver_products \
#     --vars '{"start_date": "2026-06-10", "end_date": "2026-06-10"}'
#
# For a date range:
#
#   ./scripts/run_dbt.sh run --select silver_products \
#     --vars '{"start_date": "2026-06-09", "end_date": "2026-06-10"}'
#
# Omitting --vars will process yesterday → today by default (dev convenience).
# =============================================================================

set -e

# Enforce running from repo root — data/ paths depend on CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Load transformations env vars
set -a
source src/transformations/.env
set +a

# Activate transformations venv
source src/transformations/.venv/bin/activate

# Warn loudly if no --vars with dates were passed
if [[ "$*" == *"--vars"* ]] && [[ "$*" == *"start_date"* ]]; then
    # Dates explicitly provided — good
    :
else
    echo "⚠️  WARNING: No explicit dates provided."
    echo "   Defaulting to: yesterday → today"
    echo "   This will reprocess all Bronze files from that window."
    echo ""
    echo "   To target specific dates use:"
    echo "   ./scripts/run_dbt.sh run --select silver_products \\"
    echo "     --vars '{\"start_date\": \"YYYY-MM-DD\", \"end_date\": \"YYYY-MM-DD\"}'"
    echo ""
fi

echo "Running dbt with target: ${DBT_TARGET:-dev}"
echo "Dev mode: ${R2_LOCAL_DEV_MODE:-False}"

# Pre-create local data directories so DuckDB COPY TO never fails
if [ "${R2_LOCAL_DEV_MODE:-False}" = "True" ]; then
  mkdir -p data/bronze data/silver/products data/silver/_manifests
fi

# Run dbt from repo root so CWD-based paths resolve correctly
dbt "$@" \
  --project-dir src/transformations \
  --profiles-dir src/transformations
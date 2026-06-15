#!/bin/bash
# scripts/build_silver_db.sh
# =============================================================================
# Creates data/silver_analytics.duckdb with persistent views over Silver Parquet.
# Always run from repo root — path is baked in as absolute so TablePlus works.
#
# Usage:
#   ./scripts/build_silver_db.sh           # dev (local Parquet files)
#   ./scripts/build_silver_db.sh --prod    # prod (Cloudflare R2)
# =============================================================================

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${REPO_ROOT}/data/silver_analytics.duckdb"
VIEWS_SQL="${REPO_ROOT}/sql/silver_views.sql"

echo "Repo root: ${REPO_ROOT}"
echo "Database:  ${DB_PATH}"

if [ "$1" == "--prod" ]; then
    # Production — R2
    if [ -z "$R2_ACCESS_KEY_ID" ] || [ -z "$R2_SECRET_ACCESS_KEY" ] || [ -z "$R2_ENDPOINT_URL" ]; then
        echo "ERROR: Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL for prod mode."
        exit 1
    fi
    SILVER_PATH="s3://pipeline-silver/silver/products/**/*.parquet"
    echo "Mode: PROD (R2)"
else
    # Development — local files
    SILVER_PATH="${REPO_ROOT}/data/silver/products/**/*.parquet"
    echo "Mode: DEV (local)"

    # Verify files exist
    if ! ls "${REPO_ROOT}"/data/silver/products/ 2>/dev/null | grep -q "domain="; then
        echo "ERROR: No Silver Parquet files found in data/silver/products/"
        echo "Run dbt first: ./scripts/run_dbt.sh run --select silver_products --vars '{\"start_date\": \"...\", \"end_date\": \"...\"}'"
        exit 1
    fi
fi

echo "Silver path: ${SILVER_PATH}"
echo ""

# Remove old database
rm -f "${DB_PATH}"

# Build the database
duckdb "${DB_PATH}" << DUCKDBEOF
-- Inject the absolute path as a persistent macro
CREATE OR REPLACE MACRO silver_path() AS '${SILVER_PATH}';

-- Load the views (silver_views.sql will override the macro line — that's fine,
-- the macro below wins because it runs first before the DROP/CREATE in the script)
DUCKDBEOF

# Now run the views script — but skip its macro line since we already set it
grep -v "CREATE OR REPLACE MACRO silver_path" "${VIEWS_SQL}" | \
grep -v "^-- DEV:" | \
grep -v "^-- PROD:" | \
duckdb "${DB_PATH}"

echo ""
echo "✅ Silver analytics database ready: ${DB_PATH}"
echo ""
echo "Connect in TablePlus:"
echo "  Type: DuckDB"
echo "  File: ${DB_PATH}"
echo ""
echo "Available views:"
echo "  silver_overview          — health summary"
echo "  silver_by_site           — per-site metrics"
echo "  silver_by_brand          — brand rankings"
echo "  silver_cross_site_eans   — price comparison opportunities"
echo "  silver_extraction_quality — extraction method breakdown"
echo "  silver_price_bands       — price distribution"
echo "  silver_recent            — latest scrape only"

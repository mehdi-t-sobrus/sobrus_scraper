# WORKFLOW.md — Complete Command Reference

This is the operational runbook for the Sobrus Scraper platform. Every command
needed to run the full pipeline, from a fresh URL discovery to a wholesale order,
organised by task.

For architecture and setup, see [README.md](README.md) and [CLAUDE.md](CLAUDE.md).

---

## Table of Contents

1. [E-commerce Pipeline (Bronze → Silver → Gold)](#1-e-commerce-pipeline)
2. [Dagster Orchestration](#2-dagster-orchestration)
3. [Grossiste (Wholesale) Workflow](#3-grossiste-wholesale-workflow)
4. [Database Maintenance](#4-database-maintenance)
5. [Silver Data Analysis](#5-silver-data-analysis)
6. [Testing & Linting](#6-testing--linting)
7. [Docker](#7-docker)

---

## 1. E-commerce Pipeline

The Bronze → Silver → Gold pipeline for the 5 e-commerce sites
(universparadiscount.ma, beautymarket.ma, cotepara.ma, beautymall.ma, parachezvous.ma).

### 1.1 Start required services

```bash
# Terminal 1 — Django backend
source src/backend/.venv/bin/activate
cd src/backend && python manage.py runserver

# Terminal 2 — Arq scraping worker (must stay running)
source src/backend/.venv/bin/activate
python -m arq scrapers.worker.WorkerSettings
```

### 1.2 Discover URLs (Bronze)

```bash
cd src/backend

# Discover + enqueue all active sites to Redis
python manage.py run_discovery

# Discover without enqueueing (inspect only)
python manage.py run_discovery --no-enqueue

# Discover specific sites only
python manage.py run_discovery --sites cotepara.ma beautymall.ma

# Reset stuck/failed URLs back to pending for re-scrape
python manage.py run_discovery --rescrape
```

### 1.3 Monitor scraping progress

```bash
python manage.py shell -c "
from scraper_admin.models import ScrapedURL
import json
counts = {s: ScrapedURL.objects.filter(status=s).count()
          for s in ['pending','in_progress','done','failed','blocked']}
print(json.dumps(counts, indent=2))
"
```

### 1.4 Transform to Silver (always specify dates)

```bash
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "2026-06-15", "end_date": "2026-06-15"}'

# Run dbt tests
./scripts/run_dbt.sh test --select silver_products
```

### 1.5 Match to Gold catalogue

```bash
# Seed with largest site first
python manage.py run_matching --site universparadiscount.ma
python manage.py run_matching --site beautymarket.ma
python manage.py run_matching --site cotepara.ma
python manage.py run_matching --site beautymall.ma
python manage.py run_matching --site parachezvous.ma

# All sites at once
python manage.py run_matching

# Preview without writing to DB
python manage.py run_matching --dry-run

# Specific date only
python manage.py run_matching --date 2026-06-15
```

---

## 2. Dagster Orchestration

Dagster automates steps 1.2–1.5 above as a single pipeline, scheduled nightly at 2am.

```bash
# Start Dagster UI (Arq worker from 1.1 must be running separately)
source src/orchestration/.venv/bin/activate
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py
# → http://localhost:3000
```

**In the UI:** click **Materialize All** to run the full pipeline, or click an
individual asset and **Materialize** to run just that step.

**From CLI:**

```bash
# Materialize a single asset
dagster asset materialize -f src/orchestration/definitions.py --select gold_matching

# Run the full nightly job manually
dagster job execute -f src/orchestration/definitions.py --job nightly_pipeline
```

**Troubleshooting — "multiple daemon" error:**

```bash
rm -rf .dagster/storage/ .dagster/schedules/
```

(Happens if `dagster dev` was run locally and Docker `dagster_daemon` both wrote
heartbeats to the same shared `.dagster/storage/` volume.)

---

## 3. Grossiste (Wholesale) Workflow

Three wholesale distributors with identical API structure. Credentials are
**never stored** — they're passed per-request from the external ERP system.

### 3.1 One-time setup — create grossiste configs

```bash
python manage.py shell -c "
from grossiste.models import GrossisteConfig
GrossisteConfig.objects.create(name='GPM', domain='https://gpm.ma')
GrossisteConfig.objects.create(name='COPHARM', domain='https://copharm.ma')
GrossisteConfig.objects.create(name='SOMAPHARM', domain='https://somapharm.ma')
"
```

Or via Admin: **http://localhost:8000/admin/grossiste/grossisteconfig/add/**
(domain + API paths only — no credentials).

### 3.2 Load product catalogue

The product list (`CODE_PRODU`, `NOM_PRODUI`, `PRIX_PHAR`, `PPM`, `FORME_PROD`, `PA`)
isn't exposed via a clean API — extract it manually via browser DevTools (F12) and
save as a `.js` or `.json` file in `data/grossiste/`.

```bash
mkdir -p data/grossiste
# Save extracted "var products = [...]" content to:
#   data/grossiste/gpm_products.js
#   data/grossiste/copharm_products.js
#   data/grossiste/somapharm_products.js

cd src/backend

# Preview without saving
python manage.py load_grossiste_file --name GPM \
  --file data/grossiste/gpm_products.js --dry-run

# Load for real
python manage.py load_grossiste_file --name GPM \
  --file data/grossiste/gpm_products.js

python manage.py load_grossiste_file --name COPHARM \
  --file data/grossiste/copharm_products.js

python manage.py load_grossiste_file --name SOMAPHARM \
  --file data/grossiste/somapharm_products.js
```

### 3.3 Match grossiste products to MasterProducts

No credentials needed — uses local DB matching only (EAN extraction + fuzzy name match).

```bash
python manage.py sync_grossiste --name GPM --match
python manage.py sync_grossiste --name COPHARM --match
python manage.py sync_grossiste --name SOMAPHARM --match

# All grossistes at once
python manage.py sync_grossiste --match
```

Review low-confidence matches in Admin: **Grossiste Products** → filter
"🔍 Needs review (low confidence)".

### 3.4 Check availability (requires credentials)

```bash
# Check specific products by code
python manage.py sync_grossiste --name GPM --check-stock \
  --codes 5230 1869 4240 \
  --username YOUR_USERNAME --password YOUR_PASSWORD

# Check a single product
python manage.py sync_grossiste --name GPM --check-stock \
  --codes 5230 \
  --username YOUR_USERNAME --password YOUR_PASSWORD
```

⚠️ Avoid running `--check-stock` without `--codes` on a full catalogue — it makes
one API call per product and can be slow or trigger rate limits.

### 3.5 Place an order (skeleton — endpoint TBD)

```bash
python manage.py sync_grossiste --name GPM --order \
  --code 5230 --qty 10 \
  --username YOUR_USERNAME --password YOUR_PASSWORD
```

### 3.6 API endpoints (for ERP integration)

These are the endpoints the external ERP system calls — credentials are passed
in the request body and never persisted:

```bash
# Check availability
curl -X POST http://localhost:8000/api/v1/grossiste/check-availability/ \
  -H "Content-Type: application/json" \
  -d '{
    "grossiste_name": "GPM",
    "username": "erp_user",
    "password": "erp_pass",
    "product_code": "5230"
  }'

# Place order (skeleton)
curl -X POST http://localhost:8000/api/v1/grossiste/order/ \
  -H "Content-Type: application/json" \
  -d '{
    "grossiste_name": "GPM",
    "username": "erp_user",
    "password": "erp_pass",
    "product_code": "5230",
    "quantity": 10,
    "notes": "Urgent restock"
  }'

# List available grossiste configs (no credentials needed)
curl http://localhost:8000/api/v1/grossiste/configs/
```

### 3.7 Admin — day-to-day operations

| Task | Where |
|---|---|
| Browse wholesale catalogue | Admin → Grossiste Products |
| Filter by stock / matched status | Sidebar filters on Grossiste Products |
| See margin vs retail market | Click any product → 📊 Pricing Intelligence panel |
| See wholesale prices on a Gold product | Admin → Master Products → click product → 🏭 Wholesale Prices inline |
| Find products selling below wholesale cost | Master Products → filter "🚨 Retail min ≤ wholesale cost" |
| Create / submit draft orders | Admin → Grossiste Orders |

---

## 4. Database Maintenance

```bash
cd src/backend

# Apply all pending migrations
python manage.py migrate

# Check for missing migrations (CI uses this)
python manage.py migrate --check

# Show migration status
python manage.py showmigrations

# Reset Gold data — DEV ONLY, destroys all matched products
python manage.py dbshell
# DELETE FROM products_dailypricelog;
# DELETE FROM products_siteproduct;
# DELETE FROM products_masterproduct;

# Reset stuck scraping jobs
redis-cli -n 0 FLUSHDB
psql pipeline_gold -c "
  UPDATE scraper_admin_scrapedurl
  SET status='pending', arq_job_id=''
  WHERE status='in_progress';
"

# Delete orphaned MasterProducts (0 site listings)
# Via Admin: Master Products → filter "⚠️ No sites (orphaned)" → select all →
#            action "🗑️ Delete orphaned masters"
```

---

## 5. Silver Data Analysis

```bash
# Build persistent DuckDB views (absolute paths baked in for TablePlus)
./scripts/build_silver_db.sh           # dev — local Parquet files
./scripts/build_silver_db.sh --prod    # prod — Cloudflare R2

# Connect in TablePlus: Type=DuckDB, File=data/silver_analytics.duckdb
# Then query: SELECT * FROM silver_overview;
#             SELECT * FROM silver_cross_site_eans LIMIT 50;

# One-shot analysis (10 result sets printed to terminal)
duckdb -c ".read sql/silver_analysis.sql"

# Generate self-contained HTML report
source src/backend/.venv/bin/activate
python sql/generate_report.py
python sql/generate_report.py --prod   # reads from R2 instead

open sql/reports/silver_report_$(date +%Y-%m-%d).html
```

---

## 6. Testing & Linting

```bash
source src/backend/.venv/bin/activate

# Run all tests (80+ passing)
pytest tests/ -v

# Run a specific test file
pytest tests/test_entity_resolution.py -v

# Run a specific test
pytest tests/test_entity_resolution.py::TestMatchingTiers::test_tier3_brand_gate -v

# Lint
ruff check src/ tests/

# Auto-fix lint issues
ruff check --fix src/ tests/
```

---

## 7. Docker

### Dev (standalone, uses your existing local TimescaleDB)

```bash
# Start everything
docker compose -f docker-compose.dev.yml up -d

# Logs
docker compose -f docker-compose.dev.yml logs -f backend
docker compose -f docker-compose.dev.yml logs -f arq_worker
docker compose -f docker-compose.dev.yml logs -f dagster_daemon

# Run management commands inside the container
docker compose -f docker-compose.dev.yml exec backend \
  python src/backend/manage.py migrate

docker compose -f docker-compose.dev.yml exec backend \
  python src/backend/manage.py createsuperuser

# Rebuild after Dockerfile/dependency changes
docker compose -f docker-compose.dev.yml build --no-cache

# Stop
docker compose -f docker-compose.dev.yml down
```

### Prod (full stack including TimescaleDB + nginx)

```bash
docker compose up -d
docker compose exec backend python manage.py migrate --no-input
docker compose exec backend python manage.py collectstatic --no-input
```

### Common fixes

```bash
# pg_hba.conf — allow Docker network to connect to local TimescaleDB (dev only)
docker exec pipeline-postgres bash -c \
  "echo 'host all all 172.16.0.0/12 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker exec pipeline-postgres psql -U pipeline_user -d pipeline_gold \
  -c "SELECT pg_reload_conf();"

# Clear HSTS cache in Chrome if localhost forces HTTPS
# chrome://net-internals/#hsts → Delete domain → localhost
```

---

## Quick Reference — Full Daily Run (manual, no Dagster)

```bash
# 1. Discover + scrape e-commerce sites
python manage.py run_discovery
# (wait for Arq worker to process the queue)

# 2. Transform to Silver
./scripts/run_dbt.sh run --select silver_products \
  --vars "{\"start_date\": \"$(date +%Y-%m-%d)\", \"end_date\": \"$(date +%Y-%m-%d)\"}"

# 3. Match to Gold
python manage.py run_matching

# 4. Sync grossiste catalogues (if files updated)
python manage.py load_grossiste_file --name GPM --file data/grossiste/gpm_products.js
python manage.py sync_grossiste --match

# 5. Check wholesale availability for key products
python manage.py sync_grossiste --name GPM --check-stock \
  --codes 5230 1869 --username $GPM_USER --password $GPM_PASS

# 6. Review in Admin
open http://localhost:8000/admin/products/masterproduct/
```

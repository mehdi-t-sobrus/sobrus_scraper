# WORKFLOW.md — Complete Command Reference

This is the operational runbook for the Sobrus Scraper platform.

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
8. [Quick Reference — Full Daily Run](#8-quick-reference--full-daily-run)

---

## 1. E-commerce Pipeline

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

# Discover + enqueue all active sites
python manage.py run_discovery

# Specific sites only
python manage.py run_discovery --sites cotepara.ma beautymall.ma

# Rescrape already-scraped URLs
python manage.py run_discovery --rescrape
```

### 1.3 Transform to Silver

```bash
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "2026-06-22", "end_date": "2026-06-22"}'

./scripts/run_dbt.sh test --select silver_products
```

### 1.4 Match to Gold catalogue

```bash
# Seed largest site first for best anchor products
python manage.py run_matching --site universparadiscount.ma
python manage.py run_matching --site beautymarket.ma
python manage.py run_matching --site cotepara.ma
python manage.py run_matching --site beautymall.ma
python manage.py run_matching --site parachezvous.ma

# All sites at once
python manage.py run_matching

# Dry run — no DB writes
python manage.py run_matching --dry-run
```

---

## 2. Dagster Orchestration

Automates steps 1.2–1.4 nightly at 2am.

```bash
source src/orchestration/.venv/bin/activate
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py
# → http://localhost:3000
```

```bash
# Materialize a single asset
dagster asset materialize -f src/orchestration/definitions.py --select gold_matching

# Run full pipeline manually
dagster job execute -f src/orchestration/definitions.py --job nightly_pipeline
```

**Fix "multiple daemon" error:**
```bash
rm -rf .dagster/storage/ .dagster/schedules/
```

---

## 3. Grossiste (Wholesale) Workflow

Three distributors (GPM, Sophasais, Lodimed) via **api.pharma.sobrus.com**.
No credentials stored — Sobrus session cookie passed per-request.

### 3.1 One-time setup

```bash
# Create grossiste configs in Admin or shell
python manage.py shell -c "
from grossiste.models import GrossisteConfig
GrossisteConfig.objects.create(name='GPM',       domain='https://gpm.ma',       sobrus_supplier_id=1)
GrossisteConfig.objects.create(name='Sophasais', domain='https://sophasais.ma', sobrus_supplier_id=1570)
GrossisteConfig.objects.create(name='Lodimed',   domain='https://lodimed.ma',   sobrus_supplier_id=346)
"
```

### 3.2 Load product catalogue

Extract the `var products = [...]` JS variable from the grossiste site via F12 DevTools
and save to `data/grossiste/`.

```bash
mkdir -p data/grossiste

# Dry run — preview without saving
python manage.py load_grossiste_file --name GPM \
  --file data/grossiste/gpm_products.js --dry-run

# Load for real (run from repo root or backend — both work)
python manage.py load_grossiste_file --name GPM \
  --file data/grossiste/gpm_products.js

python manage.py load_grossiste_file --name Sophasais \
  --file data/grossiste/sophasais_products.js

python manage.py load_grossiste_file --name Lodimed \
  --file data/grossiste/lodimed_products.js
```

### 3.3 Sync Sobrus product IDs

Required before availability checks and orders can work.

```bash
# Via API (requires Sobrus session cookie from the browser)
curl -X POST http://localhost:8000/api/v1/grossiste/sync-sobrus-ids/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-api-key-changeme" \
  -d '{
    "grossiste_name": "GPM",
    "sobrus_cookie": "current_country_code=ma; SBSID2=YOUR_SESSION_ID",
    "csrf_token": "YOUR_CSRF_TOKEN"
  }'
```

### 3.4 Match grossiste products to MasterProducts

No credentials needed — uses local DB (EAN extraction + fuzzy name matching).

```bash
python manage.py sync_grossiste --name GPM --match
python manage.py sync_grossiste --name Sophasais --match
python manage.py sync_grossiste --name Lodimed --match

# All at once
python manage.py sync_grossiste --match
```

Review low-confidence matches: Admin → Grossiste Products → filter "🔍 Needs review".

### 3.5 Check availability (via Sobrus API)

Requires the user's Sobrus session cookie — get it from browser DevTools (F12 → Network
→ any request to api.pharma.sobrus.com → copy the Cookie header).

```bash
# Via API endpoint
curl -X POST http://localhost:8000/api/v1/grossiste/check-availability/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-api-key-changeme" \
  -d '{
    "grossiste_name": "GPM",
    "sobrus_product_id": 148194,
    "sobrus_cookie": "current_country_code=ma; SBSID2=YOUR_SESSION_ID",
    "csrf_token": "YOUR_CSRF_TOKEN"
  }'

# Response:
# {
#   "grossiste": "GPM",
#   "sobrus_product_id": 148194,
#   "product_name": "3D VIT GOUTTE 10ML",
#   "supplier_id": 1,
#   "is_available": true,
#   "prix_pharmacien": 55.93,
#   "raw_response": {"supplierId": 1, "isAvailable": true}
# }
```

### 3.6 Place an order (via Sobrus API)

```bash
curl -X POST http://localhost:8000/api/v1/grossiste/order/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-api-key-changeme" \
  -d '{
    "grossiste_name": "GPM",
    "sobrus_product_id": 148194,
    "quantity": 10,
    "sobrus_cookie": "current_country_code=ma; SBSID2=YOUR_SESSION_ID",
    "csrf_token": "YOUR_CSRF_TOKEN",
    "owner_id": "20032742"
  }'

# Response includes:
# - order_id (our internal DB ID)
# - sobrus_transaction_num (e.g. "BC-3579")
# - sobrus_status (e.g. "approved")
# - raw_response (full Sobrus API response)
```

**Note:** `unit_price` and `sale_price` are auto-filled from `GrossisteProduct.prix_pharmacien`
and `ppm` if not provided. Override by including them in the request.

### 3.7 List available grossiste configs

```bash
curl http://localhost:8000/api/v1/grossiste/configs/ \
  -H "Authorization: Bearer dev-api-key-changeme"
```

### 3.8 How to get your Sobrus cookie

1. Open **app.pharma.sobrus.com** in Chrome
2. Log in to your Sobrus account
3. Open DevTools (F12) → Network tab
4. Click any page — find a request to `api.pharma.sobrus.com`
5. In Request Headers, copy the full **Cookie** value
6. Also copy the **X-CSRF-TOKEN** header value
7. Use both in API requests above

### 3.9 Admin operations

| Task | Where |
|---|---|
| Add/edit grossiste configs | Admin → Grossiste Configs |
| Browse wholesale catalogue | Admin → Grossiste Products |
| Filter by stock / matched / Sobrus ID | Sidebar filters |
| See margin vs retail market | Click any product → 📊 Pricing Intelligence panel |
| See wholesale prices on Gold product | Admin → Master Products → product → 🏭 Wholesale inline |
| Find products selling below wholesale | Master Products → filter "🚨 Retail min ≤ wholesale cost" |
| View order history | Admin → Grossiste Orders |

---

## 4. Database Maintenance

```bash
cd src/backend

# Apply all pending migrations
python manage.py migrate

# Check migration status
python manage.py showmigrations grossiste
python manage.py showmigrations products

# Reset stuck scraping jobs
redis-cli -n 0 FLUSHDB
psql pipeline_gold -c "
  UPDATE scraper_admin_scrapedurl
  SET status='pending', arq_job_id=''
  WHERE status='in_progress';"
```

---

## 5. Silver Data Analysis

```bash
# Build persistent DuckDB views
./scripts/build_silver_db.sh           # dev
./scripts/build_silver_db.sh --prod    # R2

# Connect in TablePlus: Type=DuckDB, File=data/silver_analytics.duckdb
# SELECT * FROM silver_overview;

# HTML report
source src/backend/.venv/bin/activate
python sql/generate_report.py
open sql/reports/silver_report_$(date +%Y-%m-%d).html
```

---

## 6. Testing & Linting

```bash
source src/backend/.venv/bin/activate

# All tests (80+ passing)
pytest tests/ -v

# Specific test files
pytest tests/test_grossiste.py -v
pytest tests/test_silver_extraction.py -v
pytest tests/test_entity_resolution.py -v
pytest tests/test_gold_layer.py -v

# Specific test class or function
pytest tests/test_grossiste.py::TestAvailabilityParsing -v
pytest tests/test_grossiste.py::TestOrderPayload::test_payload_structure -v

# Lint
ruff check src/ tests/

# Auto-fix lint
ruff check --fix src/ tests/
```

### What each test file covers

| File | Coverage |
|---|---|
| `test_grossiste.py` | Product list parsing, availability response, order payload, name normalisation, Sobrus headers |
| `test_silver_extraction.py` | JSON-LD extraction, OG meta, price normalisation (MAD/DH), description cleanup |
| `test_entity_resolution.py` | Name normalisation, volume extraction, 6-tier matching, brand gate, same-domain exclusion |
| `test_gold_layer.py` | Price comparison min/max/avg, image selection priority, MAD price regression |

---

## 7. Docker

### Dev (uses local TimescaleDB)

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml logs -f backend
docker compose -f docker-compose.dev.yml exec backend \
  python src/backend/manage.py migrate
docker compose -f docker-compose.dev.yml down
```

### Prod

```bash
docker compose up -d
docker compose exec backend python manage.py migrate --no-input
docker compose exec backend python manage.py collectstatic --no-input
```

### Common fixes

```bash
# HSTS cache forcing HTTPS on localhost
# chrome://net-internals/#hsts → Delete domain → localhost

# host.docker.internal not resolving
# Add to docker-compose service: extra_hosts: ["host.docker.internal:host-gateway"]

# Dagster multiple daemon error
rm -rf .dagster/storage/ .dagster/schedules/
```

---

## 8. Quick Reference — Full Daily Run

```bash
source src/backend/.venv/bin/activate
cd src/backend

# E-commerce pipeline
python manage.py run_discovery
# (wait for Arq worker to finish)
./scripts/run_dbt.sh run --select silver_products \
  --vars "{\"start_date\": \"$(date +%Y-%m-%d)\", \"end_date\": \"$(date +%Y-%m-%d)\"}"
python manage.py run_matching

# Grossiste
python manage.py load_grossiste_file --name GPM --file data/grossiste/gpm_products.js
python manage.py sync_grossiste --match

# Review in Admin
open http://localhost:8000/admin/products/masterproduct/
open http://localhost:8000/admin/grossiste/grossisteproduct/
```

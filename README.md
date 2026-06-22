# Sobrus Scraper — Moroccan Parapharmacy Price Comparison Platform

A production-grade web scraping and price comparison system that tracks products across Moroccan parapharmacy and e-commerce websites. The platform automatically discovers products, scrapes prices, normalises data, and resolves product identities across sites to answer: **"Where is this product cheapest right now?"**

> 📋 **For the complete command reference** (every task, every command, organised as a runbook), see **[WORKFLOW.md](WORKFLOW.md)**.

## Architecture Overview

```
Bronze (Raw HTML)  →  Silver (Clean Parquet)  →  Gold (Canonical Catalogue)
   curl_cffi             dbt + DuckDB              Django + PostgreSQL
   Arq workers           JSON-LD / OG / CSS        Entity Resolution
   Cloudflare R2         price normalisation        TimescaleDB + pgvector
```

**Orchestration:** Dagster runs the full pipeline nightly at 2am.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web Framework & ORM | Django 5.1 + Django Ninja |
| Orchestration | Dagster 1.13 |
| Scraping Queue | Redis + Arq |
| Scraping Engine | `curl_cffi` (TLS/JA3 Chrome impersonation) + `selectolax` |
| Transformation | `dbt-duckdb` (Python models, Parquet output) |
| Gold Warehouse | PostgreSQL 17 + TimescaleDB + pgvector |
| Embeddings | `paraphrase-multilingual-mpnet-base-v2` (sentence-transformers) |
| Storage | Cloudflare R2 / local `data/` in dev |
| Deployment | Docker / Docker Compose on Hetzner |

---

## Supported Sites

| Site | Platform | Products |
|---|---|---|
| universparadiscount.ma | PrestaShop | ~14,700 |
| beautymarket.ma | Shopify | ~11,700 |
| cotepara.ma | WooCommerce + Yoast | ~12,900 |
| beautymall.ma | WooCommerce + Yoast | ~13,600 |
| parachezvous.ma | WooCommerce + Rank Math | ~10,200 |

Total: **~57,000+ product URLs** across 5 sites.

---

## Project Structure

```
sobrus_scraper/
├── .github/workflows/ci.yml       # CI/CD — lint, test, migrations, deploy
├── .dagster/dagster.yaml           # Dagster instance config
├── docker/                         # Dockerfiles + entrypoints + nginx
│   ├── Dockerfile.backend          # Shared image: Django + Arq worker
│   ├── Dockerfile.dagster          # Dagster webserver + daemon + dbt
│   └── entrypoint.*.sh             # Per-service startup scripts
├── scripts/
│   ├── run_dbt.sh                  # Silver transformation runner
│   ├── run_worker.sh               # Arq worker launcher
│   └── build_silver_db.sh         # Creates DuckDB views for TablePlus/analysis
├── sql/
│   ├── silver_analysis.sql         # One-shot Silver data analysis (DuckDB)
│   ├── silver_views.sql            # Persistent DuckDB views
│   ├── generate_report.py          # Generates HTML report from Silver data
│   └── reports/                    # Generated HTML reports (gitignored)
├── src/
│   ├── backend/                    # Django project (own .venv)
│   │   ├── core/                   # Settings, URLs, API router, health check
│   │   ├── products/               # Gold: MasterProduct, SiteProduct, DailyPriceLog
│   │   └── scraper_admin/          # SiteConfig, ScrapedURL, ProxyPool
│   ├── scrapers/                   # Bronze: discovery + Arq workers
│   │   └── plugins/sites/          # shopify.py, woocommerce.py, universparadiscount.py
│   ├── transformations/            # Silver: dbt-DuckDB (own .venv)
│   ├── matching/                   # Gold: 6-tier entity resolution
│   └── orchestration/              # Dagster assets + schedules (own .venv)
├── tests/
│   ├── test_silver_extraction.py   # JSON-LD, OG meta, price normalisation
│   ├── test_entity_resolution.py   # Matching tiers, brand gate, same-domain exclusion
│   └── test_gold_layer.py          # Price comparison, image selection, description cleanup
├── data/                           # Local dev data (gitignored)
├── .env.example                    # Root env template for Docker
├── docker-compose.yml              # Production (7 services)
├── docker-compose.dev.yml          # Development (standalone, uses host TimescaleDB)
└── pyproject.toml                  # Root package + [tool.dagster] config
```

---

## Quick Start — Docker (Recommended)

### Prerequisites
- Docker Desktop
- Existing TimescaleDB + pgvector running locally (or use the prod `docker-compose.yml` which starts its own)

### 1. Clone and configure

```bash
git clone https://github.com/your-org/sobrus_scraper.git
cd sobrus_scraper
cp .env.example .env
```

Edit `.env` — minimum required for dev:

```env
DATABASE_URL=postgresql://pipeline_user:yourpassword@host.docker.internal:5432/pipeline_gold
DJANGO_SECRET_KEY=dev-secret-key-not-for-production
DJANGO_API_KEY=dev-api-key-changeme
REDIS_URL=redis://redis:6379/0
R2_LOCAL_DEV_MODE=True
R2_ENDPOINT_URL=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
DAGSTER_HOME=/app/.dagster
```

### 2. Allow Docker to connect to local PostgreSQL

```bash
# Find your TimescaleDB container
docker ps | grep timescale

# Add Docker network range to pg_hba.conf
docker exec <container> bash -c \
  "echo 'host all all 172.16.0.0/12 md5' >> /var/lib/postgresql/data/pg_hba.conf"
docker exec <container> psql -U pipeline_user -d pipeline_gold -c "SELECT pg_reload_conf();"
```

### 3. Start all services

```bash
docker compose -f docker-compose.dev.yml up -d

# Watch logs
docker compose -f docker-compose.dev.yml logs -f backend
```

### 4. Create superuser and add sites

```bash
docker compose -f docker-compose.dev.yml exec backend \
  python src/backend/manage.py createsuperuser
```

Open http://localhost:8000/admin → **Site Configurations** → Add each site.

### Access points
| Service | URL |
|---|---|
| Django Admin | http://localhost:8000/admin |
| API docs | http://localhost:8000/api/v1/docs |
| Dagster UI | http://localhost:3000 |

---

## Quick Start — Local (without Docker)

```bash
# Backend venv
python3.13 -m venv src/backend/.venv
source src/backend/.venv/bin/activate
pip install -r src/backend/requirements.txt
pip install -e .

# Configure
cp src/backend/.env.example src/backend/.env
# Edit src/backend/.env with your local DB credentials

# Migrate and run
cd src/backend
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

---

## Running the Pipeline

### Via Dagster UI (recommended)
Click **Materialize All** in the asset graph at http://localhost:3000.

The Arq worker must be running separately:
```bash
source src/backend/.venv/bin/activate
python -m arq scrapers.worker.WorkerSettings
```

### Manually

```bash
# 1. Discover URLs (enqueues to Redis)
python src/backend/manage.py run_discovery

# 2. Silver transformation (always specify dates)
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "2026-06-15", "end_date": "2026-06-15"}'

# 3. Gold matching (largest site first to seed catalogue)
python src/backend/manage.py run_matching --site universparadiscount.ma
python src/backend/manage.py run_matching --site beautymarket.ma
python src/backend/manage.py run_matching --site cotepara.ma
python src/backend/manage.py run_matching --site beautymall.ma
python src/backend/manage.py run_matching --site parachezvous.ma
```

---

## Entity Resolution — How Matching Works

| Tier | Method | Action |
|---|---|---|
| 1 | EAN/GTIN exact match | Auto-match |
| 2 | SKU + domain exact match | Auto-match |
| 3 | Normalised name token-sort ≥ 0.95 + brand gate | Auto-match |
| 3.5 | Brand + volume + key token fingerprint | Auto-match |
| 4 | pgvector cosine ≥ 0.90 + brand gate | Auto-match |
| 5 | pgvector cosine 0.65–0.89 | Flag for review |
| 6 | No match | Create new MasterProduct |

**Hard rules:** same site never merges with itself; different brands never match.

---

## Price Comparison

**Django Admin:** Any Master Product → **💰 Price Comparison** panel.

**API:**
```bash
# Single product
GET /api/v1/products/master/{id}/price-comparison/

# Full catalogue
GET /api/v1/products/price-comparison/?multi_site_only=true&brand=ISDIN

Authorization: Bearer your-api-key
```

---

## Silver Data Analysis

```bash
# Build persistent DuckDB views (absolute path baked in for TablePlus)
./scripts/build_silver_db.sh

# Connect in TablePlus: Type=DuckDB, File=data/silver_analytics.duckdb
# Then query:
#   SELECT * FROM silver_overview;
#   SELECT * FROM silver_cross_site_eans LIMIT 50;

# One-shot analysis printed to terminal
duckdb -c ".read sql/silver_analysis.sql"

# Generate HTML report
source src/backend/.venv/bin/activate
python sql/generate_report.py
open sql/reports/silver_report_$(date +%Y-%m-%d).html
```

---

## Testing

```bash
source src/backend/.venv/bin/activate
pytest tests/ -v
```

**56+ tests covering:**
- Silver extraction: JSON-LD variants, Open Graph meta, price normalisation (MAD/EUR/dirham), description cleanup
- Entity resolution: all matching tiers, same-domain exclusion, brand gate, fingerprinting
- Gold layer: price comparison calculations, image selection priority, description cleanup per site

---

## Adding a New Site

**Shopify:**
```python
# discoverer.py
"newstore.ma": (ShopifyPlugin, {}),
```
Add CSS selectors in `silver_products.py` + SiteConfig in Admin. No new plugin code.

**WooCommerce:** Same with `WooCommercePlugin`.

**New platform:** Implement `BaseDiscoveryPlugin` in `src/scrapers/plugins/sites/`.

---

## Proxy Configuration

Admin → **Proxy Pool** → Add. Set endpoint as full DSN: `http://user:pass@host:port`.

- Leave **sites** empty → global proxy (all sites)
- Select specific sites → site-restricted proxy (e.g. residential for cotepara.ma)

In dev with no proxies configured, scrapers use the host IP.

---

## CI/CD

GitHub Actions on every push:

| Job | Description |
|---|---|
| `lint` | Ruff linting |
| `test` | Full pytest suite (56+ tests) |
| `migrations` | Runs all migrations against real TimescaleDB+pgvector |
| `deploy` | SSH deploy to Hetzner (main branch only) |

Required GitHub secrets: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY`.

---

## Environment Variables

See `.env.example` at the repo root for the full annotated reference.

Key variables:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `DJANGO_SECRET_KEY` | ✅ | `openssl rand -hex 32` |
| `DJANGO_API_KEY` | ✅ | Bearer token for API auth |
| `REDIS_URL` | ✅ | Redis connection string |
| `R2_LOCAL_DEV_MODE` | | `True` = use local `data/` instead of R2 |
| `R2_ENDPOINT_URL` | prod | Cloudflare R2 endpoint (leave empty in dev) |

---

## Licence

Private — all rights reserved.

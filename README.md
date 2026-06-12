# Sobrus Scraper — Moroccan Parapharmacy Price Comparison Platform

A production-grade web scraping and price comparison system that tracks products across Moroccan parapharmacy and e-commerce websites. The platform automatically discovers products, scrapes prices, normalises data, and resolves product identities across sites to answer: **"Where is this product cheapest right now?"**

## Architecture Overview

The system uses a **Medallion Architecture** with three data layers:

```
Bronze (Raw HTML)  →  Silver (Clean Parquet)  →  Gold (Canonical Catalogue)
   curl_cffi             dbt + DuckDB              Django + PostgreSQL
   Arq workers           JSON-LD / OG / CSS        Entity Resolution
   Cloudflare R2         price normalisation        TimescaleDB + pgvector
```

**Orchestration:** Dagster runs the full pipeline nightly at 2am — discover → scrape → transform → match.

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

Total: **~57,000+ product URLs** across 5 sites, growing to 20+ sites.

---

## Prerequisites

- Python 3.13+
- PostgreSQL 17 with [TimescaleDB](https://docs.timescale.com/install/latest/) and [pgvector](https://github.com/pgvector/pgvector)
- Redis 7+
- macOS: `brew install postgresql@17 timescaledb pgvector redis`

---

## Project Structure

```
sobrus_scraper/
├── .dagster/dagster.yaml         # Dagster instance config
├── .github/workflows/ci.yml      # CI/CD pipeline
├── scripts/
│   ├── run_dbt.sh                # Silver transformation runner
│   └── run_worker.sh             # Arq worker launcher
├── src/
│   ├── backend/                  # Django project (own .venv)
│   │   ├── core/                 # Settings, URLs, API router
│   │   ├── products/             # Gold: MasterProduct, SiteProduct, DailyPriceLog
│   │   └── scraper_admin/        # SiteConfig, ScrapedURL, ProxyPool
│   ├── scrapers/                 # Bronze: discovery + Arq workers
│   │   └── plugins/sites/        # shopify.py, woocommerce.py, universparadiscount.py
│   ├── transformations/          # Silver: dbt-DuckDB models (own .venv)
│   │   └── models/silver/        # silver_products.py (3-strategy extraction)
│   ├── matching/                 # Gold: entity_res.py (6-tier matching)
│   └── orchestration/            # Dagster assets + schedules (own .venv)
├── tests/
│   ├── test_silver_extraction.py # JSON-LD, OG meta, price normalisation tests
│   └── test_entity_resolution.py # Matching tier tests
├── data/                         # Local dev data (gitignored)
│   ├── bronze/                   # Raw .jsonl.gz files
│   └── silver/                   # Parquet files (domain/date partitioned)
├── pyproject.toml                # Root package definition
├── pytest.ini                    # Test config
└── CLAUDE.md                     # Architecture & coding guidelines
```

---

## Quick Start

### 1. Clone and set up environment

```bash
git clone https://github.com/your-org/sobrus_scraper.git
cd sobrus_scraper

# Create backend venv (used by Django + Arq worker + matching)
python3.13 -m venv src/backend/.venv
source src/backend/.venv/bin/activate
pip install -r src/backend/requirements.txt
pip install -e .
```

### 2. Configure environment variables

```bash
cp src/backend/.env.example src/backend/.env
cp src/scrapers/.env.example src/scrapers/.env
cp src/transformations/.env.example src/transformations/.env
cp src/matching/.env.example src/matching/.env
cp src/orchestration/.env.example src/orchestration/.env
```

Edit `src/backend/.env` at minimum:

```env
DJANGO_SECRET_KEY=your-secret-key-here   # openssl rand -hex 32
DATABASE_URL=postgresql://user:pass@localhost:5432/pipeline_gold
REDIS_URL=redis://localhost:6379/0
```

### 3. Set up the database

```bash
createdb pipeline_gold
psql pipeline_gold -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
psql pipeline_gold -c "CREATE EXTENSION IF NOT EXISTS vector;"

cd src/backend
python manage.py migrate
python manage.py createsuperuser
```

### 4. Start services

```bash
# Terminal 1 — Django backend
source src/backend/.venv/bin/activate
cd src/backend && python manage.py runserver

# Terminal 2 — Arq scraping worker (long-running)
source src/backend/.venv/bin/activate
python -m arq scrapers.worker.WorkerSettings

# Terminal 3 — Dagster UI
source src/orchestration/.venv/bin/activate
pip install -r src/orchestration/requirements.txt
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py
# → http://localhost:3000
```

### 5. Add sites in Django Admin

Open http://localhost:8000/admin → **Site Configurations** → Add each site with its domain and base URL, status = Active.

### 6. Run the pipeline

**Via Dagster UI:** Click **Materialize All** in the asset graph.

**Manually:**

```bash
cd src/backend

# Discover + enqueue all URLs
python manage.py run_discovery

# Silver transformation (always specify dates)
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}'

# Gold matching (run largest site first to seed catalogue)
python manage.py run_matching --site universparadiscount.ma
python manage.py run_matching --site beautymarket.ma
python manage.py run_matching --site cotepara.ma
python manage.py run_matching --site beautymall.ma
python manage.py run_matching --site parachezvous.ma
```

---

## Entity Resolution

Products from different sites are linked to a single canonical `MasterProduct` using a 6-tier pipeline:

| Tier | Method | Confidence | Action |
|---|---|---|---|
| 1 | EAN/GTIN exact match | 100% | Auto-match |
| 2 | SKU + domain exact match | 100% | Auto-match |
| 3 | Normalised name token-sort ≥ 0.95 + brand gate | Very high | Auto-match |
| 3.5 | Brand + volume + key token fingerprint | High | Auto-match |
| 4 | pgvector cosine ≥ 0.90 + brand gate | High | Auto-match |
| 5 | pgvector cosine 0.65–0.89 | Medium | Flag for human review |
| 6 | No match | Low | Create new MasterProduct |

**Hard rules:** same site never merges with itself; different brands never match.

---

## Price Comparison

**Django Admin:** Any Master Product → **💰 Price Comparison** panel shows min/avg/max across all sites with the cheapest highlighted.

**API:**

```bash
# Single product
GET /api/v1/products/master/{id}/price-comparison/

# Full catalogue (products on 2+ sites)
GET /api/v1/products/price-comparison/?multi_site_only=true&brand=ISDIN

# Auth
Authorization: Bearer your-api-key
```

---

## Testing

```bash
source src/backend/.venv/bin/activate
pip install pytest

pytest tests/ -v
```

Tests cover Silver extraction (JSON-LD variants, OG meta, price normalisation, description cleanup) and entity resolution (all matching tiers, same-domain exclusion, brand gate).

---

## Adding a New Site

**Shopify store:**
```python
# discoverer.py PLUGIN_REGISTRY
"newstore.ma": (ShopifyPlugin, {}),
```
Then add CSS selectors in `silver_products.py` and a SiteConfig row in Admin. No new plugin code needed.

**WooCommerce store:** Same as above with `WooCommercePlugin`.

**New platform:** Implement `BaseDiscoveryPlugin` in `src/scrapers/plugins/sites/`.

---

## Proxy Configuration

Add proxies via Admin → **Proxy Pool** → Add, with a full DSN (`http://user:pass@host:port`). Workers pick them up automatically. In dev mode with no proxies configured, the host IP is used.

---

## CI/CD

GitHub Actions runs lint, tests, and migrations check on every push. Deploys to Hetzner on merges to `main`.

Required secrets: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY`.

---

## Environment Variables

### `src/backend/.env`

| Variable | Required | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | ✅ | `openssl rand -hex 32` |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `REDIS_URL` | ✅ | Redis connection string |
| `DJANGO_DEBUG` | | `True` for dev |
| `DJANGO_API_KEY` | | Bearer token for API auth |

### `src/scrapers/.env` + `src/transformations/.env`

| Variable | Description |
|---|---|
| `R2_LOCAL_DEV_MODE` | `True` = use local `data/` instead of R2 |
| `R2_ENDPOINT_URL` | Cloudflare R2 endpoint (production) |
| `R2_ACCESS_KEY_ID` | R2 credentials (production) |
| `R2_SECRET_ACCESS_KEY` | R2 credentials (production) |

---

## Licence

Private — all rights reserved.
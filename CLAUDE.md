# CLAUDE.md — Project Guidelines & Architecture

## Project Overview

An enterprise-grade web scraping and price comparison platform tracking products
across Moroccan parapharmacy and e-commerce websites. The system uses a decoupled
**Medallion Architecture (Bronze → Silver → Gold)**. Django serves as the core data
management layer (ORM, Admin Portal, API Backend), Dagster orchestrates the pipeline,
and Arq handles high-speed async scraping.

**Current sites:** 5 (universparadiscount.ma, beautymarket.ma, cotepara.ma,
beautymall.ma, parachezvous.ma) — targeting 20+ sites and 400,000+ products.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web Framework & ORM | Django 5.1 + Django Ninja (async API) |
| Orchestration | Dagster 1.13 (asset-based, nightly schedule) |
| Scraping Queue | Redis + Arq (async worker queue) |
| Scraping Engine | `curl_cffi` (TLS/JA3 Chrome impersonation) + `selectolax` |
| Transformation | `dbt-duckdb` (Python models, Parquet output) |
| Gold Warehouse | PostgreSQL 17 + TimescaleDB + pgvector |
| Storage | Cloudflare R2 / local `data/` in dev |
| Deployment | Docker / Docker Compose on Hetzner bare-metal |

---

## Repository Structure

```
sobrus_scraper/
├── .dagster/
│   └── dagster.yaml              # Dagster instance config (SQLite local)
├── .github/workflows/            # CI/CD (pending)
├── scripts/
│   ├── run_dbt.sh                # Runs dbt from repo root — always specify dates
│   └── run_worker.sh             # Starts Arq worker
├── src/
│   ├── backend/                  # Django project (own .venv)
│   │   ├── manage.py
│   │   ├── requirements.txt
│   │   ├── core/                 # settings, urls, asgi, api router
│   │   ├── products/             # Gold warehouse models + Admin + API
│   │   │   ├── models.py         # MasterProduct, SiteProduct, DailyPriceLog
│   │   │   ├── admin.py          # Price comparison panel, orphan cleanup
│   │   │   ├── api.py            # /price-comparison/ endpoints
│   │   │   └── migrations/       # 0001–0007
│   │   └── scraper_admin/        # SiteConfig, ScrapedURL, ScrapeLog, ProxyPool
│   │       └── management/commands/
│   │           ├── run_discovery.py   # Dagster calls this via subprocess
│   │           └── run_matching.py    # Dagster calls this via subprocess
│   ├── scrapers/                 # Bronze layer (own .venv shared with backend)
│   │   ├── discoverer.py         # Sitemap discovery → ScrapedURL DB
│   │   ├── worker.py             # Arq workers → Bronze .jsonl.gz files
│   │   └── plugins/
│   │       ├── base.py           # BaseDiscoveryPlugin
│   │       └── sites/
│   │           ├── shopify.py         # Generic Shopify (beautymarket.ma + future)
│   │           ├── woocommerce.py     # Generic WooCommerce (cotepara, beautymall, parachezvous)
│   │           └── universparadiscount.py  # PrestaShop one-off
│   ├── transformations/          # Silver layer (own .venv)
│   │   ├── requirements.txt
│   │   ├── dbt_project.yml
│   │   ├── profiles.yml
│   │   └── models/silver/
│   │       ├── silver_products.py     # 3-strategy extraction: JSON-LD > OG meta > CSS
│   │       └── silver_products.yml
│   ├── matching/                 # Gold entity resolution (uses backend .venv)
│   │   └── entity_res.py         # 6-tier matching engine + image selection
│   └── orchestration/            # Dagster (own .venv)
│       ├── requirements.txt
│       ├── definitions.py        # Dagster entry point
│       ├── assets/
│       │   ├── bronze.py         # bronze_urls + bronze_scraping assets
│       │   ├── silver.py         # silver_products asset
│       │   └── gold.py           # gold_matching asset
│       ├── schedules/daily.py    # 2am nightly schedule
│       └── resources/pipeline.py # PipelineConfig resource
├── tests/                        # pytest (pending)
├── data/                         # Local dev data (gitignored)
│   ├── bronze/                   # Raw .jsonl.gz files
│   └── silver/                   # Parquet files partitioned by domain/date
├── pyproject.toml                # Root package — registers all src/* packages
└── CLAUDE.md                     # This file
```

---

## Virtual Environments

Three separate venvs — never mix them:

| venv | Location | Used by |
|---|---|---|
| Backend | `src/backend/.venv` | Django, Arq worker, matching |
| Transformations | `src/transformations/.venv` | dbt + DuckDB |
| Orchestration | `src/orchestration/.venv` | Dagster |

Install all from repo root:
```bash
pip install -e .   # registers all packages (core, scrapers, matching, orchestration, etc.)
```

---

## Key Commands

### Development

```bash
# Django backend
source src/backend/.venv/bin/activate
cd src/backend && python manage.py runserver

# Arq scraping worker (keep running in a separate terminal)
python -m arq scrapers.worker.WorkerSettings

# Dagster UI
source src/orchestration/.venv/bin/activate
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py
# → http://localhost:3000
```

### Pipeline (manual — Dagster does this automatically)

```bash
# 1. Discover URLs for all sites (+ enqueue to Redis)
python manage.py run_discovery

# 2. Discover without enqueueing (dry run / inspection)
python manage.py run_discovery --no-enqueue

# 3. Silver transformation — ALWAYS specify dates to avoid reprocessing
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}'

# 4. Gold matching
python manage.py run_matching --site universparadiscount.ma
python manage.py run_matching  # all sites

# Dry run (no DB writes)
python manage.py run_matching --dry-run
```

### Database

```bash
# Apply all migrations
python manage.py migrate

# Reset Gold data (development only)
python manage.py dbshell
# DELETE FROM products_dailypricelog;
# DELETE FROM products_siteproduct;
# DELETE FROM products_masterproduct;

# Reset Redis queue + stuck in_progress URLs
redis-cli -n 0 FLUSHDB
psql pipeline_gold -c "UPDATE scraper_admin_scrapedurl SET status='pending', arq_job_id='' WHERE status='in_progress';"
```

### dbt

```bash
source src/transformations/.venv/bin/activate
cd src/transformations

dbt run --select silver_products --vars '{"start_date": "...", "end_date": "..."}'
dbt test
dbt docs generate && dbt docs serve
```

---

## Medallion Architecture

### Bronze (Raw)
- **What:** Arq workers fetch product pages and write raw HTML to `.jsonl.gz` files
- **Where:** `data/bronze/domain=<site>/date=<date>/` (dev) or Cloudflare R2 (prod)
- **Rules:** Workers NEVER write to DB directly. Workers NEVER parse HTML content.
- **Trigger:** Dagster `bronze_urls` asset discovers URLs → enqueues to Redis → `bronze_scraping` monitors

### Silver (Cleaned)
- **What:** dbt-DuckDB reads Bronze files, extracts product fields, writes Parquet
- **Where:** `data/silver/products/domain=<site>/fetched_date=<date>/` (dev) or R2 (prod)
- **Extraction:** 3 strategies in priority order:
  1. JSON-LD (`<script type="application/ld+json">`) — primary
  2. Open Graph meta tags (`product:price:amount`, `product:retailer_item_id` for EAN) — secondary
  3. CSS selectors (site-specific fallback)
- **Rules:** Never overwrite Bronze files. Specify `start_date`/`end_date` vars always.

### Gold (Canonical)
- **What:** Entity resolution matches Silver records to a canonical `MasterProduct` catalogue
- **Where:** PostgreSQL 17 + TimescaleDB (Django ORM)
- **Matching tiers:**
  1. EAN/GTIN exact match → auto (100%)
  2. SKU + site exact match → auto (100%)
  3. Normalised name token-sort ≥ 0.95 + brand gate → auto
  4. Brand + volume + key token fingerprint → auto
  5. pgvector cosine ≥ 0.90 + brand gate → auto
  6. pgvector cosine 0.65–0.89 → flag for human review
  7. No match → create new MasterProduct
- **Rules:** Same site NEVER merges with itself. Different brands NEVER match.
  `DailyPriceLog` FKs are `SET_NULL` (not CASCADE) — price history is immutable.

---

## Django Models (Gold Warehouse)

### `MasterProduct`
Canonical product identity. One row per unique real-world product.
Fields: `name`, `brand`, `ean`, `mpn`, `category`, `description`, `image_urls` (JSON),
`tags` (JSON), `status`, `match_confidence`, `manually_verified`, `name_embedding` (pgvector 768d).

### `SiteProduct`
Per-site listing linked to a MasterProduct. One row per (product, site).
Fields: `master_product` FK, `site` FK, `raw_name`, `raw_brand`, `raw_ean`,
`current_price`, `currency`, `in_stock`, `image_url`, `product_url`, `match_score`.
Constraint: `unique_together (master_product, site)`.

### `DailyPriceLog`
Append-only time-series of every price observation (TimescaleDB hypertable).
FKs are `SET_NULL` — never cascade delete. Partitioned on `logged_at` (30-day chunks).
Compression after 90 days. Continuous aggregate: `daily_price_summary` view.

---

## Site Registry

| Domain | Platform | Plugin | Notes |
|---|---|---|---|
| universparadiscount.ma | PrestaShop | `UniversparadiscountPlugin` | gzip sitemap, XML sanitisation |
| beautymarket.ma | Shopify | `ShopifyPlugin` | 5 product sitemaps with `?from=&to=` |
| cotepara.ma | WooCommerce+Yoast | `WooCommercePlugin` | product-sitemap.xml, EAN via OG |
| beautymall.ma | WooCommerce+Yoast | `WooCommercePlugin` | 14 sitemaps, priceSpecification, gtin13 |
| parachezvous.ma | WooCommerce+RankMath | `WooCommercePlugin` | 51 sitemaps, additionalProperty, reviews |

**Adding a new Shopify site:** one line in `PLUGIN_REGISTRY` + SiteConfig in Admin.
**Adding a new WooCommerce site:** one line in `PLUGIN_REGISTRY` + SiteConfig in Admin.
**Adding a new PrestaShop site:** refactor `universparadiscount.py` → `prestashop.py` first.

---

## Proxy Setup

Proxies are configured per-site in the Django Admin under **Proxy Pool**.
Each `ProxyPool` record has: `url`, `username`, `password`, `is_active`, `site` (optional FK).

In dev mode all proxy fields are empty — scrapers fall back to the host IP.

To add a proxy in production:
1. Admin → Proxy Pool → Add
2. Set `url` (e.g. `http://proxy.provider.com:8080`), credentials, `is_active=True`
3. Optionally link to a specific `SiteConfig` for site-specific routing
4. The scraper worker picks up active proxies automatically on next run

Concurrency limits per domain are set on `SiteConfig.max_concurrency` (default: 5).
Never exceed 5 concurrent connections per domain without explicit approval.

---

## Coding Standards

### Separation of Concerns
- **Orchestration layer** (Dagster): calls management commands via subprocess — never imports Django apps directly
- **Matching layer**: reads Silver Parquet → writes to Django ORM — never reads Bronze
- **Scraping layer**: writes Bronze only — never reads DB, never parses HTML
- **Transformation layer**: reads Bronze → writes Silver — never touches DB

### Python Standards
- All new functions: strict type hints + docstrings
- Async DB operations: use Django's async ORM (`await Model.objects.aget(...)`) or `sync_to_async`
- Async networking: `curl_cffi.requests.AsyncSession` with `impersonate="chrome"` only — never `requests` or `BeautifulSoup4`
- DB writes: check-then-act pattern — never rely on catching constraint violations

### Database Rules
- `DailyPriceLog`: insert-only. Never UPDATE or DELETE rows directly.
- TimescaleDB hypertables: managed via raw SQL migrations — not standard Django migrations
- pgvector embeddings: read/write via raw SQL (`cursor.execute`) — not ORM
- Silver Parquet: immutable. Never overwrite Bronze or Silver files.
- Constraint violations: use check-then-act, never try/except on DB errors

### Concurrency
- Max 5 concurrent connections per domain (configurable via `SiteConfig.max_concurrency`)
- Arq worker runs as a separate long-lived process — Dagster monitors it, never owns it

---

## Dagster Pipeline

Asset graph: `bronze_urls → bronze_scraping → silver_products → gold_matching`

```bash
# Launch (from repo root)
source src/orchestration/.venv/bin/activate
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py

# Materialize one asset only
dagster asset materialize -f src/orchestration/definitions.py --select gold_matching
```

Schedule: nightly at 2am (`nightly_pipeline_schedule`).
The Arq worker must be running independently for `bronze_scraping` to complete.

---

## API Endpoints

Base URL: `http://localhost:8000/api/v1/`
Auth: Bearer token (`DJANGO_API_KEY`) or Django session.

| Method | Path | Description |
|---|---|---|
| GET | `/products/master/` | List MasterProducts |
| GET | `/products/master/{id}/price-comparison/` | Price comparison for one product |
| GET | `/products/price-comparison/` | Full listing with min/max/avg/cheapest |
| GET | `/products/master/{id}/price-history/` | Price history time series |
| GET | `/scrapers/sites/` | List SiteConfigs |
| GET | `/scrapers/urls/` | List ScrapedURLs |

---

## Pending Items

- [ ] Docker — `Dockerfile.backend`, `Dockerfile.worker`, `Dockerfile.dagster`, `docker-compose.yml`
- [ ] Tests — pytest for parsers, price normalisation, entity resolution tiers
- [ ] CI/CD — `.github/workflows/deploy.yml`
- [ ] Add proxies to ProxyPool for bot-protected sites (cotepara.ma, beautymall.ma)
- [ ] R2 Silver reading in `entity_res.py` (currently `R2_LOCAL_DEV_MODE=True` only)
- [ ] Public-facing price comparison frontend (API is ready)
- [ ] Tier 4 vector matching verification on new sites

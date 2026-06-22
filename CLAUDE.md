# CLAUDE.md — Project Guidelines & Architecture

## Project Overview

A production-grade web scraping and price comparison platform tracking products across
Moroccan parapharmacy and e-commerce websites. Uses a decoupled **Medallion Architecture
(Bronze → Silver → Gold)**. Django serves as the core data management layer (ORM, Admin,
API), Dagster orchestrates the pipeline, and Arq handles async scraping.

**Current sites:** 5 (universparadiscount.ma, beautymarket.ma, cotepara.ma, beautymall.ma,
parachezvous.ma) — targeting 20+ sites and 400,000+ products.

**Current data:** ~57,000 URLs discovered, ~11,896 MasterProducts, ~17,000 SiteProducts,
~34,000 DailyPriceLog entries.

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
| Embeddings | `paraphrase-multilingual-mpnet-base-v2` (sentence-transformers, 768d) |
| Storage | Cloudflare R2 / local `data/` in dev |
| Deployment | Docker / Docker Compose on Hetzner |

---

## Repository Structure

```
sobrus_scraper/
├── .dagster/dagster.yaml             # Dagster instance config (SQLite local)
├── .github/workflows/ci.yml          # CI/CD — lint, test, migrations, deploy
├── .dockerignore
├── .env.example                       # Root env template for Docker
├── .env                               # Dev values (gitignored)
├── conftest.py                        # pytest path setup (runs before all tests)
├── pytest.ini                         # pytest config
├── ruff.toml                          # Ruff linting config (ignores E402, E501)
├── pyproject.toml                     # Root package + [tool.dagster] config
├── docker-compose.yml                 # Production (7 services)
├── docker-compose.dev.yml             # Dev standalone (uses host TimescaleDB)
├── docker/
│   ├── Dockerfile.backend             # Shared: Django + Arq worker
│   ├── Dockerfile.dagster             # Dagster webserver + daemon + dbt
│   ├── entrypoint.backend.sh          # wait for DB → migrate → gunicorn
│   ├── entrypoint.worker.sh           # wait for Redis + backend → arq
│   ├── entrypoint.dagster_web.sh      # wait for DB → dagster-webserver
│   ├── entrypoint.dagster_daemon.sh   # wait for webserver → dagster-daemon
│   ├── postgres/init/01_extensions.sql
│   └── nginx/nginx.conf
├── scripts/
│   ├── run_dbt.sh                     # Silver transformation runner
│   ├── run_worker.sh                  # Arq worker launcher
│   └── build_silver_db.sh            # Creates DuckDB views with absolute paths
├── sql/
│   ├── silver_analysis.sql            # One-shot DuckDB analysis (dev/prod toggle)
│   ├── silver_views.sql               # Persistent DuckDB views (MACRO-based)
│   ├── generate_report.py             # Generates HTML report from Silver data
│   └── reports/                       # Generated HTML reports (gitignored)
├── src/
│   ├── backend/                       # Django project (own .venv)
│   │   ├── requirements.txt
│   │   ├── core/                      # settings, urls, asgi, api router, health check
│   │   ├── products/                  # Gold: MasterProduct, SiteProduct, DailyPriceLog
│   │   │   ├── models.py
│   │   │   ├── admin.py               # Price comparison panel, orphan cleanup
│   │   │   ├── api.py                 # /price-comparison/ endpoints
│   │   │   └── migrations/            # 0001–0007
│   │   └── scraper_admin/             # SiteConfig, ScrapedURL, ProxyPool
│   │       ├── models.py
│   │       ├── admin.py
│   │       ├── api.py
│   │       └── migrations/            # 0001–0003
│   ├── scrapers/                      # Bronze layer (shared backend .venv)
│   │   ├── discoverer.py              # Sitemap discovery → ScrapedURL DB
│   │   ├── worker.py                  # Arq workers → Bronze .jsonl.gz files
│   │   └── plugins/
│   │       ├── base.py                # BaseDiscoveryPlugin
│   │       └── sites/
│   │           ├── shopify.py         # beautymarket.ma + future Shopify sites
│   │           ├── woocommerce.py     # cotepara, beautymall, parachezvous
│   │           └── universparadiscount.py
│   ├── transformations/               # Silver layer (own .venv)
│   │   ├── requirements.txt
│   │   ├── dbt_project.yml
│   │   ├── profiles.yml
│   │   └── models/silver/
│   │       ├── __init__.py            # Required for Python imports in tests
│   │       └── silver_products.py     # 3-strategy extraction: JSON-LD > OG > CSS
│   ├── matching/                      # Gold entity resolution (uses backend .venv)
│   │   └── entity_res.py              # 6-tier matching + image selection
│   └── orchestration/                 # Dagster (own .venv)
│       ├── definitions.py
│       ├── assets/
│       │   ├── bronze.py              # bronze_urls + bronze_scraping assets
│       │   ├── silver.py              # silver_products asset
│       │   └── gold.py                # gold_matching asset
│       ├── schedules/daily.py         # 2am nightly schedule
│       └── resources/pipeline.py      # PipelineConfig resource
├── tests/
│   ├── test_silver_extraction.py      # JSON-LD, OG meta, price normalisation, descriptions
│   ├── test_entity_resolution.py      # Matching tiers, brand gate, same-domain exclusion
│   └── test_gold_layer.py             # Price comparison, image selection, MAD prices
└── data/                              # Local dev data (gitignored)
    ├── bronze/                        # Raw .jsonl.gz files
    ├── silver/                        # Parquet files (domain/date partitioned)
    └── silver_analytics.duckdb        # Persistent DuckDB views (gitignored)
```

---

## Virtual Environments

Three separate venvs — never mix them:

| venv | Location | Used by |
|---|---|---|
| Backend | `src/backend/.venv` | Django, Arq worker, matching, tests |
| Transformations | `src/transformations/.venv` | dbt + DuckDB |
| Orchestration | `src/orchestration/.venv` | Dagster |

---

## Key Commands

### Local Development (without Docker)

```bash
# Django backend
source src/backend/.venv/bin/activate
cd src/backend && python manage.py runserver

# Arq scraping worker (separate terminal — always running)
python -m arq scrapers.worker.WorkerSettings

# Dagster UI
source src/orchestration/.venv/bin/activate
export DAGSTER_HOME=$(pwd)/.dagster
dagster dev -f src/orchestration/definitions.py
# → http://localhost:3000
```

### Docker Dev Stack

```bash
# Start (uses host TimescaleDB via host.docker.internal)
docker compose -f docker-compose.dev.yml up -d

# Logs
docker compose -f docker-compose.dev.yml logs -f backend
docker compose -f docker-compose.dev.yml logs -f arq_worker

# Stop
docker compose -f docker-compose.dev.yml down

# Rebuild after Dockerfile changes
docker compose -f docker-compose.dev.yml build --no-cache
```

Docker services: redis, backend (Django), arq_worker, dagster_web, dagster_daemon
Access: Admin http://localhost:8000/admin | API docs http://localhost:8000/api/v1/docs | Dagster http://localhost:3000

### Pipeline (manual)

```bash
# 1. Discover URLs + enqueue to Redis
python manage.py run_discovery

# 2. Silver (always specify dates)
./scripts/run_dbt.sh run --select silver_products \
  --vars '{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}'

# 3. Gold matching (largest site first)
python manage.py run_matching --site universparadiscount.ma
python manage.py run_matching  # all sites
python manage.py run_matching --dry-run  # no DB writes
```

### Dagster

```bash
# Materialize single asset (skips rest of pipeline)
dagster asset materialize -f src/orchestration/definitions.py --select gold_matching

# Clear stale daemon heartbeats (if "multiple daemon" error)
rm -rf .dagster/storage/ .dagster/schedules/
```

### Database

```bash
# Reset Gold data (dev only)
python manage.py dbshell
# DELETE FROM products_dailypricelog;
# DELETE FROM products_siteproduct;
# DELETE FROM products_masterproduct;

# Reset stuck scraping jobs
redis-cli -n 0 FLUSHDB
psql pipeline_gold -c "UPDATE scraper_admin_scrapedurl SET status='pending', arq_job_id='' WHERE status='in_progress';"
```

### Silver Data Analysis

```bash
# Build DuckDB views with absolute paths (for TablePlus)
./scripts/build_silver_db.sh

# Connect in TablePlus: Type=DuckDB, File=data/silver_analytics.duckdb
# Query: SELECT * FROM silver_overview;
#        SELECT * FROM silver_cross_site_eans LIMIT 50;

# One-shot analysis
duckdb -c ".read sql/silver_analysis.sql"

# HTML report
source src/backend/.venv/bin/activate
python sql/generate_report.py
open sql/reports/silver_report_$(date +%Y-%m-%d).html
```

### Tests & Lint

```bash
source src/backend/.venv/bin/activate

# All tests (80 passing)
pytest tests/ -v

# Lint
ruff check src/ tests/

# Auto-fix lint
ruff check --fix src/ tests/
```

---

## Medallion Architecture

### Bronze (Raw)
- Arq workers fetch product pages → write raw HTML to `.jsonl.gz` files
- Location: `data/bronze/domain=<site>/date=<date>/` (dev) or Cloudflare R2 (prod)
- **Rules:** Workers NEVER write to DB directly. Workers NEVER parse HTML.
- Dagster `bronze_urls` discovers URLs + enqueues to Redis. `bronze_scraping` monitors progress.

### Silver (Cleaned)
- dbt-DuckDB reads Bronze → extracts product fields → writes Parquet
- Location: `data/silver/products/domain=<site>/fetched_date=<date>/`
- **Extraction order:** JSON-LD → Open Graph meta → CSS selectors
- **Price normalisation:** handles MAD, DH, د.م. symbols
- **Description cleanup:** strips Rank Math price embeds, delivery boilerplate, site suffixes
- **Rules:** Never overwrite Bronze. Always specify `start_date`/`end_date` vars.
- `silver_products.py` uses lazy imports for boto3/pandas/selectolax so tests can import
  pure functions (`_clean_description`, `_normalise_price` etc.) without those packages.

### Gold (Canonical)
- Entity resolution matches Silver records to canonical `MasterProduct` catalogue
- Location: PostgreSQL 17 + TimescaleDB (Django ORM)

**6-tier matching pipeline:**
1. EAN/GTIN exact match → auto (100%)
2. SKU + domain exact match → auto (100%)
3. Normalised name token-sort ≥ 0.95 + brand gate → auto
4. Brand + volume + key token fingerprint (e.g. `isdin_200ml_fotoprotector`) → auto
5. pgvector cosine ≥ 0.90 + brand gate → auto
6. pgvector cosine 0.65–0.89 → flag review; below 0.65 → new MasterProduct

**Hard rules:** Same site NEVER merges with itself. Different brands NEVER match.

**Image selection priority:** parachezvous.ma > beautymall.ma > cotepara.ma > beautymarket.ma > universparadiscount.ma

---

## Django Models (Gold Warehouse)

### `MasterProduct`
Canonical product. Fields: `name`, `brand`, `ean`, `mpn`, `category`, `description`,
`image_urls` (JSON), `tags` (JSON), `status`, `match_confidence`, `manually_verified`,
`name_embedding` (pgvector 768d).

### `SiteProduct`
Per-site listing. Fields: `master_product` FK, `site` FK, `raw_name`, `raw_brand`,
`raw_ean`, `current_price`, `currency`, `in_stock`, `image_url`, `product_url`,
`match_score`. Constraint: `unique_together (master_product, site)`.

### `DailyPriceLog`
Append-only TimescaleDB hypertable. FKs are `SET_NULL` (not CASCADE) — price history
is never destroyed when products are reorganised. Admin: read-only, no add/change/delete.

### `ProxyPool`
Rotating proxy endpoints. Has M2M to `SiteConfig` for per-site proxy routing.
Empty sites = global proxy. Site-specific proxies take priority over global.

---

## Site Registry

| Domain | Platform | Plugin | Notes |
|---|---|---|---|
| universparadiscount.ma | PrestaShop | `UniversparadiscountPlugin` | gzip sitemap |
| beautymarket.ma | Shopify | `ShopifyPlugin` | 5 product sitemaps with `?from=&to=` |
| cotepara.ma | WooCommerce+Yoast | `WooCommercePlugin` | EAN via OG retailer_item_id |
| beautymall.ma | WooCommerce+Yoast | `WooCommercePlugin` | 14 sitemaps, gtin13, brand-as-array |
| parachezvous.ma | WooCommerce+RankMath | `WooCommercePlugin` | 51 sitemaps, additionalProperty |

**Adding Shopify/WooCommerce site:** one line in `PLUGIN_REGISTRY` + SiteConfig in Admin.

---

## Proxy Configuration

Configured via Django Admin → **Proxy Pool**. Leave fields empty in dev — scrapers
fall back to host IP (warning logged).

Worker proxy priority: site-specific → global → host IP.

Recommended providers for bot-protected Moroccan sites: BrightData, Oxylabs, IPRoyal.
DSN format: `http://username:password@gateway_host:port`

---

## Price Comparison Feature

**Django Admin:** Master Product detail → **💰 Price Comparison** panel shows:
- Min / Avg / Max / Max Saving % summary bar
- Per-site table: image, site name, price, stock badge, "View →" link
- 🏆 Cheapest badge on winning site

**API endpoints:**
- `GET /api/v1/products/master/{id}/price-comparison/`
- `GET /api/v1/products/price-comparison/?multi_site_only=true&brand=ISDIN&in_stock_only=true`
- `GET /api/v1/health/` — public, no auth (Docker healthcheck)

---

## Docker Architecture

### Dev (`docker-compose.dev.yml` — standalone)
Services: redis, backend, arq_worker, dagster_web, dagster_daemon
- Uses existing local TimescaleDB via `host.docker.internal:5432`
- Source code mounted as volumes (hot reload)
- Root `.env` must have `DATABASE_URL` pointing to `host.docker.internal`
- No nginx in dev

### Prod (`docker-compose.yml`)
Services: db (TimescaleDB), redis, backend, arq_worker, dagster_web, dagster_daemon, nginx
- `db` service runs TimescaleDB container with `docker/postgres/init/01_extensions.sql`
- nginx reverse proxy on port 80
- Static files served via nginx

### Common Issues
- **"multiple daemon" error in Dagster:** `rm -rf .dagster/storage/ .dagster/schedules/`
- **pg_hba.conf for Docker:** add `host all all 172.16.0.0/12 md5` BEFORE catch-all line
- **host.docker.internal not resolving:** add `extra_hosts: - "host.docker.internal:host-gateway"` to service

---

## CI/CD (.github/workflows/ci.yml)

| Job | Description |
|---|---|
| `lint` | `ruff check src/ tests/` |
| `test` | `pytest tests/ -v` — 80 tests |
| `migrations` | Runs all migrations against `timescale/timescaledb:latest-pg17` + pgvector |
| `deploy` | SSH to Hetzner on `main` branch merge |

Required GitHub secrets: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY`

**Known CI issues being fixed:**
- `W292 No newline at end of file` in `tests/test_silver_extraction.py`
- `ModuleNotFoundError: No module named 'models'` — needs `conftest.py` at repo root
  and `src/transformations/models/__init__.py` + `src/transformations/models/silver/__init__.py`

---

## Environment Variables

Root `.env` (Docker Compose reads this — different from `src/backend/.env`):

| Variable | Dev value | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql://pipeline_user:localpassword@host.docker.internal:5432/pipeline_gold` | Points to local TimescaleDB |
| `DJANGO_SECRET_KEY` | `dev-secret-key-not-for-production` | |
| `DJANGO_API_KEY` | `dev-api-key-changeme` | Bearer token for API |
| `REDIS_URL` | `redis://redis:6379/0` | `redis` = Docker service name |
| `R2_LOCAL_DEV_MODE` | `True` | Use local `data/` instead of R2 |
| `R2_ENDPOINT_URL` | *(empty)* | Must be empty in dev |
| `DAGSTER_HOME` | `/app/.dagster` | |

---

## Coding Standards

- **Async DB:** use Django async ORM (`await Model.objects.aget(...)`) or `sync_to_async`
- **Networking:** `curl_cffi.requests.AsyncSession` with `impersonate="chrome"` only
- **Type hints:** all new functions must have strict type hints + docstrings
- **DB writes:** check-then-act pattern — never rely on catching constraint violations
- **Bronze:** workers write only, never read or parse
- **Silver:** dbt reads Bronze, writes Parquet — never touches DB
- **Gold:** reads Silver Parquet, writes to Django ORM — never reads Bronze
- **Orchestration:** Dagster calls management commands via subprocess — never imports Django directly

---

## Pending Items

- [ ] Fix CI tests: `conftest.py` not picked up / `models` module not found in CI
- [ ] Fix CI lint: `W292` trailing newline in `test_silver_extraction.py`
- [ ] Create Cloudflare R2 buckets (`pipeline-bronze`, `pipeline-silver`)
- [ ] Provision Hetzner server (CPX31 — 4 vCPU, 8GB RAM, ~€12.49/month)
- [ ] Production Docker deployment
- [ ] Add proxies for bot-protected sites (cotepara.ma, beautymall.ma)
- [ ] Public-facing price comparison frontend (REST API is ready)

## Completed Items

- [x] Bronze → Silver → Gold pipeline end to end
- [x] 5 sites with platform plugins (Shopify, WooCommerce x3, PrestaShop)
- [x] 6-tier entity resolution with pgvector + sentence-transformers
- [x] Price comparison Admin panel (min/avg/max, cheapest site, images)
- [x] REST API price comparison endpoints
- [x] Dagster orchestration with nightly 2am schedule
- [x] Docker dev setup (standalone compose, uses host TimescaleDB)
- [x] Docker prod setup (full 7-service compose)
- [x] Proxy infrastructure (model + per-site routing + worker integration)
- [x] R2 Silver reading in entity_res.py with ETag caching
- [x] DailyPriceLog SET_NULL FKs (price history preserved on product deletion)
- [x] Silver SQL analysis tools (DuckDB views, HTML report generator)
- [x] CI/CD GitHub Actions (lint, test, migrations, deploy)
- [x] 80 tests passing locally
- [x] README + CLAUDE.md documentation
- [x] Manager presentation (8-slide deck, budget ~€70-90/month)
- [x] ruff.toml (ignores E402, E501, migrations)
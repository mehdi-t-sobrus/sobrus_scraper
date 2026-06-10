"""
src/backend/products/migrations/0002_dailypricelog_hypertable.py
=================================================================
Promotes DailyPriceLog to a TimescaleDB hypertable partitioned on logged_at.

Applies the same PK drop-and-recompose pattern as scraper_admin/0002:
    1. Drop the implicit unique index on `id` (BigAutoField PK).
    2. create_hypertable() on logged_at.
    3. Re-add PK as composite (id, logged_at).

See CLAUDE.md §3 — do NOT run standard Django deletes/updates on this table.
"""

from django.db import migrations


UPGRADE_SQL = """
-- Step 1: Drop the single-column PK — TimescaleDB rejects unique indexes
-- that don't include the partition column (logged_at).
ALTER TABLE products_dailypricelog DROP CONSTRAINT products_dailypricelog_pkey;

-- Step 2: Promote to hypertable.
SELECT create_hypertable(
    'products_dailypricelog',
    'logged_at',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- Step 3: Re-add PK as composite (id, logged_at).
ALTER TABLE products_dailypricelog
    ADD CONSTRAINT products_dailypricelog_pkey PRIMARY KEY (id, logged_at);

-- Step 4: Compression policy (chunks older than 90 days).
ALTER TABLE products_dailypricelog SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'logged_at DESC',
    timescaledb.compress_segmentby = 'master_product_id, site_id'
);

SELECT add_compression_policy(
    'products_dailypricelog',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- Step 5: Continuous aggregate — daily min/max/avg price per product per site.
-- Query this view for analytics instead of scanning raw hypertable rows.
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_price_summary
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', logged_at)   AS day,
    master_product_id,
    site_id,
    MIN(price)                         AS price_min,
    MAX(price)                         AS price_max,
    AVG(price)                         AS price_avg,
    BOOL_OR(in_stock)                  AS any_in_stock
FROM products_dailypricelog
GROUP BY day, master_product_id, site_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'daily_price_summary',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);
"""

DOWNGRADE_SQL = "SELECT 1;"  # Intentionally irreversible — see CLAUDE.md §3


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=UPGRADE_SQL, reverse_sql=DOWNGRADE_SQL),
    ]

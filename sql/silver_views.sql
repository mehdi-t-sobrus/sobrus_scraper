-- =============================================================================
-- silver_views.sql — Persistent DuckDB views over Silver Parquet
-- =============================================================================
-- Run once to create, then query like regular tables forever.
--
-- DEV:  duckdb data/silver_analytics.duckdb < sql/silver_views.sql
-- PROD: set R2 env vars then:
--       duckdb /opt/silver_analytics.duckdb < sql/silver_views.sql
--
-- After running, open:
--   duckdb data/silver_analytics.duckdb
-- And query:
--   SELECT * FROM silver_overview;
--   SELECT * FROM silver_by_site;
--   SELECT * FROM silver_cross_site_eans LIMIT 50;
-- =============================================================================

-- ---------------------------------------------------------------------------
-- PATH MACRO — persists in the .duckdb file between sessions
-- Unlike SET VARIABLE, macros survive closing and reopening the connection.
-- ---------------------------------------------------------------------------

-- DEV: local Parquet files (default)
CREATE OR REPLACE MACRO silver_path() AS 'data/silver/products/**/*.parquet';

-- PROD: comment the line above, uncomment below
-- INSTALL httpfs;
-- LOAD httpfs;
-- SET s3_endpoint          = getenv('R2_ENDPOINT_URL');
-- SET s3_access_key_id     = getenv('R2_ACCESS_KEY_ID');
-- SET s3_secret_access_key = getenv('R2_SECRET_ACCESS_KEY');
-- SET s3_region            = 'auto';
-- SET s3_url_style         = 'path';
-- CREATE OR REPLACE MACRO silver_path() AS 's3://pipeline-silver/silver/products/**/*.parquet';


-- ---------------------------------------------------------------------------
-- Drop existing views
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS silver_raw;
DROP VIEW IF EXISTS silver_overview;
DROP VIEW IF EXISTS silver_by_site;
DROP VIEW IF EXISTS silver_by_brand;
DROP VIEW IF EXISTS silver_cross_site_eans;
DROP VIEW IF EXISTS silver_extraction_quality;
DROP VIEW IF EXISTS silver_price_bands;
DROP VIEW IF EXISTS silver_recent;


-- ===========================================================================
-- BASE VIEW — all records with derived columns
-- ===========================================================================

CREATE VIEW silver_raw AS
SELECT
    domain,
    fetched_date,
    scraped_url_id,
    site_id,
    url,
    raw_name,
    raw_brand,
    raw_price,
    raw_currency,
    raw_description,
    raw_ean,
    raw_sku,
    raw_mpn,
    raw_images,
    in_stock,
    raw_stock_qty,
    raw_rating,
    raw_review_count,
    raw_category,
    raw_tags,
    raw_attributes,
    extraction_method,
    parse_errors,
    fetched_at_utc,
    bronze_source_key,
    to_timestamp(fetched_at_utc)                        AS fetched_at,
    raw_price IS NOT NULL                               AS has_price,
    raw_ean IS NOT NULL AND raw_ean != ''               AS has_ean,
    raw_brand IS NOT NULL AND raw_brand != ''           AS has_brand,
    parse_errors != '[]' AND parse_errors IS NOT NULL   AS has_errors
FROM read_parquet(silver_path(), hive_partitioning=true);


-- ===========================================================================
-- OVERVIEW — single-row health summary
-- ===========================================================================

CREATE VIEW silver_overview AS
SELECT
    COUNT(*)                                            AS total_records,
    COUNT(DISTINCT domain)                              AS total_sites,
    COUNT(DISTINCT fetched_date)                        AS total_dates,
    MIN(fetched_date)                                   AS earliest_date,
    MAX(fetched_date)                                   AS latest_date,
    COUNT(DISTINCT raw_ean) FILTER (WHERE has_ean)      AS unique_eans,
    COUNT(DISTINCT raw_brand) FILTER (WHERE has_brand)  AS unique_brands,
    ROUND(AVG(has_price::INT) * 100, 1)                AS price_coverage_pct,
    ROUND(AVG(has_ean::INT) * 100, 1)                  AS ean_coverage_pct,
    ROUND(AVG(has_brand::INT) * 100, 1)                AS brand_coverage_pct,
    ROUND(AVG(in_stock::INT) * 100, 1)                 AS in_stock_pct,
    ROUND(AVG(raw_price) FILTER (WHERE raw_price > 0), 2) AS global_avg_price_mad
FROM silver_raw;


-- ===========================================================================
-- BY SITE
-- ===========================================================================

CREATE VIEW silver_by_site AS
SELECT
    domain,
    COUNT(*)                                            AS records,
    MIN(fetched_date)                                   AS first_scraped,
    MAX(fetched_date)                                   AS last_scraped,
    ROUND(MIN(raw_price) FILTER (WHERE raw_price > 0), 2) AS min_price,
    ROUND(AVG(raw_price) FILTER (WHERE raw_price > 0), 2) AS avg_price,
    ROUND(MAX(raw_price), 2)                            AS max_price,
    ROUND(AVG(has_price::INT) * 100, 1)                AS price_pct,
    ROUND(AVG(has_ean::INT) * 100, 1)                  AS ean_pct,
    ROUND(AVG(has_brand::INT) * 100, 1)                AS brand_pct,
    ROUND(AVG(in_stock::INT) * 100, 1)                 AS in_stock_pct,
    ROUND(AVG(has_errors::INT) * 100, 1)               AS error_pct,
    COUNT_IF(extraction_method LIKE '%json_ld%')        AS via_json_ld,
    COUNT_IF(extraction_method LIKE '%og_meta%')        AS via_og_meta,
    COUNT_IF(extraction_method LIKE '%css%')            AS via_css_only
FROM silver_raw
GROUP BY domain
ORDER BY records DESC;


-- ===========================================================================
-- BY BRAND
-- ===========================================================================

CREATE VIEW silver_by_brand AS
SELECT
    raw_brand                                           AS brand,
    COUNT(DISTINCT domain)                              AS sites_count,
    COUNT(*)                                            AS total_listings,
    LIST(DISTINCT domain ORDER BY domain)               AS found_on,
    ROUND(MIN(raw_price) FILTER (WHERE raw_price > 0), 2) AS min_price,
    ROUND(AVG(raw_price) FILTER (WHERE raw_price > 0), 2) AS avg_price,
    ROUND(MAX(raw_price), 2)                            AS max_price,
    ROUND(AVG(has_ean::INT) * 100, 1)                  AS ean_pct
FROM silver_raw
WHERE has_brand
GROUP BY raw_brand
ORDER BY total_listings DESC;


-- ===========================================================================
-- CROSS-SITE EANs — products on 2+ sites
-- ===========================================================================

CREATE VIEW silver_cross_site_eans AS
SELECT
    raw_ean,
    COUNT(DISTINCT domain)                              AS site_count,
    LIST(DISTINCT domain ORDER BY domain)               AS sites,
    ANY_VALUE(raw_name)                                 AS sample_name,
    ANY_VALUE(raw_brand)                                AS brand,
    ROUND(MIN(raw_price) FILTER (WHERE raw_price > 0), 2) AS min_price,
    ROUND(MAX(raw_price), 2)                            AS max_price,
    ROUND(MAX(raw_price) - MIN(raw_price)
        FILTER (WHERE raw_price > 0), 2)               AS price_spread,
    ROUND((1 - MIN(raw_price) FILTER (WHERE raw_price > 0)
        / NULLIF(MAX(raw_price), 0)) * 100, 1)         AS saving_pct,
    ANY_VALUE(raw_currency)                             AS currency
FROM silver_raw
WHERE has_ean AND LENGTH(raw_ean) BETWEEN 8 AND 14
GROUP BY raw_ean
HAVING COUNT(DISTINCT domain) >= 2
ORDER BY price_spread DESC NULLS LAST;


-- ===========================================================================
-- EXTRACTION QUALITY
-- ===========================================================================

CREATE VIEW silver_extraction_quality AS
SELECT
    domain,
    extraction_method,
    COUNT(*)                                            AS records,
    ROUND(COUNT(*) * 100.0
        / SUM(COUNT(*)) OVER (PARTITION BY domain), 1) AS pct_of_site,
    ROUND(AVG(has_price::INT) * 100, 1)                AS price_pct,
    ROUND(AVG(has_ean::INT) * 100, 1)                  AS ean_pct
FROM silver_raw
GROUP BY domain, extraction_method
ORDER BY domain, records DESC;


-- ===========================================================================
-- PRICE BANDS
-- ===========================================================================

CREATE VIEW silver_price_bands AS
SELECT
    domain,
    CASE
        WHEN raw_price < 50   THEN '1. < 50 MAD'
        WHEN raw_price < 100  THEN '2. 50-100 MAD'
        WHEN raw_price < 200  THEN '3. 100-200 MAD'
        WHEN raw_price < 500  THEN '4. 200-500 MAD'
        WHEN raw_price < 1000 THEN '5. 500-1000 MAD'
        ELSE                       '6. > 1000 MAD'
    END                                                 AS price_band,
    COUNT(*)                                            AS products,
    ROUND(AVG(raw_price), 2)                            AS avg_price
FROM silver_raw
WHERE raw_price > 0
GROUP BY domain, price_band
ORDER BY domain, price_band;


-- ===========================================================================
-- RECENT — latest scrape date per site only
-- ===========================================================================

CREATE VIEW silver_recent AS
SELECT s.*
FROM silver_raw s
INNER JOIN (
    SELECT domain, MAX(fetched_date) AS latest_date
    FROM silver_raw
    GROUP BY domain
) latest ON s.domain = latest.domain
         AND s.fetched_date = latest.latest_date;


-- ---------------------------------------------------------------------------
-- Confirmation
-- ---------------------------------------------------------------------------

SELECT '=== VIEWS READY ===' AS info;

SELECT table_name AS view,
       'SELECT * FROM ' || table_name || ' LIMIT 20;' AS example
FROM information_schema.tables
WHERE table_type = 'VIEW'
ORDER BY table_name;

SELECT '=== HEALTH CHECK ===' AS info;
SELECT * FROM silver_overview;

SELECT '=== PER-SITE ===' AS info;
SELECT domain, records, avg_price, ean_pct, in_stock_pct FROM silver_by_site;

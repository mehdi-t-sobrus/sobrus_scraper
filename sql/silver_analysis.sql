-- =============================================================================
-- silver_analysis.sql — One-shot Silver data analysis
-- =============================================================================
-- Place at repo root. Run from repo root.
--
-- DEV (local Parquet files):
--   duckdb -c ".read silver_analysis.sql"
--
-- PROD (Cloudflare R2):
--   export R2_ACCESS_KEY_ID=your_key
--   export R2_SECRET_ACCESS_KEY=your_secret
--   export R2_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
--   duckdb -c "
--     INSTALL httpfs; LOAD httpfs;
--     SET s3_endpoint=getenv('R2_ENDPOINT_URL');
--     SET s3_access_key_id=getenv('R2_ACCESS_KEY_ID');
--     SET s3_secret_access_key=getenv('R2_SECRET_ACCESS_KEY');
--     SET s3_region='auto';
--     SET s3_url_style='path';
--     .read silver_analysis.sql
--   "
-- =============================================================================

-- ---------------------------------------------------------------------------
-- PATH CONFIGURATION — comment/uncomment to switch between dev and prod
-- ---------------------------------------------------------------------------

-- DEV: local Parquet files (default)
CREATE OR REPLACE MACRO silver_path() AS 'data/silver/products/**/*.parquet';

-- PROD: uncomment these lines and comment the DEV line above
-- INSTALL httpfs;
-- LOAD httpfs;
-- SET s3_endpoint          = getenv('R2_ENDPOINT_URL');
-- SET s3_access_key_id     = getenv('R2_ACCESS_KEY_ID');
-- SET s3_secret_access_key = getenv('R2_SECRET_ACCESS_KEY');
-- SET s3_region            = 'auto';
-- SET s3_url_style         = 'path';
-- CREATE OR REPLACE MACRO silver_path() AS 's3://pipeline-silver/silver/products/**/*.parquet';


-- ===========================================================================
-- 1. OVERVIEW
-- ===========================================================================

SELECT '=== SILVER OVERVIEW ===' AS section;

SELECT
    COUNT(*)                                        AS total_records,
    COUNT(DISTINCT domain)                          AS sites,
    COUNT(DISTINCT fetched_date)                    AS dates_covered,
    MIN(fetched_date)                               AS earliest_date,
    MAX(fetched_date)                               AS latest_date,
    ROUND(SUM(raw_price IS NOT NULL)::FLOAT / COUNT(*) * 100, 1) AS price_coverage_pct,
    ROUND(SUM(raw_ean != '' AND raw_ean IS NOT NULL)::FLOAT / COUNT(*) * 100, 1) AS ean_coverage_pct,
    ROUND(SUM(in_stock)::FLOAT / COUNT(*) * 100, 1) AS in_stock_pct
FROM read_parquet(silver_path(), hive_partitioning=true);


-- ===========================================================================
-- 2. PER-SITE BREAKDOWN
-- ===========================================================================

SELECT '=== PER-SITE BREAKDOWN ===' AS section;

SELECT
    domain,
    COUNT(*)                                                        AS records,
    ROUND(AVG(raw_price), 2)                                        AS avg_price,
    MIN(raw_price)                                                  AS min_price,
    MAX(raw_price)                                                  AS max_price,
    SUM(raw_price IS NOT NULL)                                      AS priced,
    SUM(raw_ean != '' AND raw_ean IS NOT NULL)                      AS with_ean,
    SUM(raw_brand != '' AND raw_brand IS NOT NULL)                  AS with_brand,
    SUM(in_stock)                                                   AS in_stock,
    ROUND(SUM(raw_ean != '' AND raw_ean IS NOT NULL)::FLOAT / COUNT(*) * 100, 1) AS ean_pct,
    COUNT_IF(extraction_method LIKE '%json_ld%')                    AS via_json_ld,
    COUNT_IF(extraction_method LIKE '%og_meta%')                    AS via_og_meta,
    COUNT_IF(extraction_method LIKE '%css%')                        AS via_css
FROM read_parquet(silver_path(), hive_partitioning=true)
GROUP BY domain
ORDER BY records DESC;


-- ===========================================================================
-- 3. EXTRACTION METHODS
-- ===========================================================================

SELECT '=== EXTRACTION METHODS ===' AS section;

SELECT
    domain,
    extraction_method,
    COUNT(*)                                AS records,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY domain), 1) AS pct_of_site
FROM read_parquet(silver_path(), hive_partitioning=true)
GROUP BY domain, extraction_method
ORDER BY domain, records DESC;


-- ===========================================================================
-- 4. PRICE DISTRIBUTION
-- ===========================================================================

SELECT '=== PRICE DISTRIBUTION ===' AS section;

SELECT
    domain,
    raw_currency,
    COUNT(*)                        AS products,
    ROUND(MIN(raw_price), 2)        AS min_price,
    ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY raw_price), 2) AS p25,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY raw_price), 2) AS median,
    ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY raw_price), 2) AS p75,
    ROUND(MAX(raw_price), 2)        AS max_price,
    ROUND(AVG(raw_price), 2)        AS avg_price
FROM read_parquet(silver_path(), hive_partitioning=true)
WHERE raw_price IS NOT NULL AND raw_price > 0
GROUP BY domain, raw_currency
ORDER BY domain;


-- ===========================================================================
-- 5. TOP BRANDS
-- ===========================================================================

SELECT '=== TOP 20 BRANDS ===' AS section;

SELECT
    raw_brand,
    COUNT(DISTINCT domain)          AS sites_count,
    COUNT(*)                        AS total_listings,
    ROUND(AVG(raw_price), 2)        AS avg_price,
    LIST(DISTINCT domain ORDER BY domain) AS found_on
FROM read_parquet(silver_path(), hive_partitioning=true)
WHERE raw_brand IS NOT NULL AND raw_brand != ''
GROUP BY raw_brand
ORDER BY total_listings DESC
LIMIT 20;


-- ===========================================================================
-- 6. EAN COVERAGE
-- ===========================================================================

SELECT '=== EAN COVERAGE ===' AS section;

SELECT
    domain,
    COUNT(*)                                        AS total,
    SUM(raw_ean != '' AND raw_ean IS NOT NULL)       AS with_ean,
    ROUND(SUM(raw_ean != '' AND raw_ean IS NOT NULL)::FLOAT / COUNT(*) * 100, 1) AS ean_pct
FROM read_parquet(silver_path(), hive_partitioning=true)
GROUP BY domain
ORDER BY ean_pct DESC;


-- ===========================================================================
-- 7. CROSS-SITE EAN MATCHES — products on 2+ sites
-- ===========================================================================

SELECT '=== CROSS-SITE EAN MATCHES (top 20 by price spread) ===' AS section;

SELECT
    raw_ean,
    COUNT(DISTINCT domain)              AS sites,
    LIST(DISTINCT domain ORDER BY domain) AS found_on,
    ANY_VALUE(raw_name)                 AS sample_name,
    ANY_VALUE(raw_brand)                AS brand,
    MIN(raw_price)                      AS min_price,
    MAX(raw_price)                      AS max_price,
    ROUND(MAX(raw_price) - MIN(raw_price), 2) AS price_spread,
    ROUND((1 - MIN(raw_price) / NULLIF(MAX(raw_price), 0)) * 100, 1) AS saving_pct
FROM read_parquet(silver_path(), hive_partitioning=true)
WHERE raw_ean IS NOT NULL
  AND raw_ean != ''
  AND LENGTH(raw_ean) BETWEEN 8 AND 14
GROUP BY raw_ean
HAVING COUNT(DISTINCT domain) >= 2
ORDER BY price_spread DESC
LIMIT 20;


-- ===========================================================================
-- 8. PARSE ERRORS
-- ===========================================================================

SELECT '=== PARSE ERRORS ===' AS section;

SELECT
    domain,
    COUNT(*)                                        AS total,
    SUM(parse_errors != '[]' AND parse_errors IS NOT NULL) AS with_errors,
    ROUND(SUM(parse_errors != '[]' AND parse_errors IS NOT NULL)::FLOAT / COUNT(*) * 100, 1) AS error_pct
FROM read_parquet(silver_path(), hive_partitioning=true)
GROUP BY domain
ORDER BY error_pct DESC;


-- ===========================================================================
-- 9. SAMPLE PRODUCTS — 10 per site
-- ===========================================================================

SELECT '=== SAMPLE PRODUCTS (10 per site) ===' AS section;

SELECT domain, raw_name, raw_brand, raw_ean, raw_price, raw_currency,
       in_stock, raw_category, extraction_method, fetched_date
FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY domain ORDER BY RANDOM()) AS rn
    FROM read_parquet(silver_path(), hive_partitioning=true)
    WHERE raw_price IS NOT NULL AND raw_name IS NOT NULL AND raw_name != ''
)
WHERE rn <= 10
ORDER BY domain, raw_brand, raw_name;


-- ===========================================================================
-- 10. DAILY PROGRESS
-- ===========================================================================

SELECT '=== DAILY SCRAPE PROGRESS ===' AS section;

SELECT
    fetched_date,
    domain,
    COUNT(*)                 AS records,
    SUM(in_stock)            AS in_stock,
    ROUND(AVG(raw_price), 2) AS avg_price
FROM read_parquet(silver_path(), hive_partitioning=true)
GROUP BY fetched_date, domain
ORDER BY fetched_date DESC, domain;

-- tests/assert_silver_extraction_failure_rate.sql
-- ==================================================
-- Singular data test: fails if more than 10% of processed rows
-- have extraction_method = 'failed' for any domain.
-- Triggers an alert to update selectors for that site.

SELECT
    domain,
    COUNT(*)                                                        AS total_rows,
    COUNT_IF(extraction_method = 'failed')                          AS failed_rows,
    ROUND(
        100.0 * COUNT_IF(extraction_method = 'failed') / COUNT(*),
        2
    )                                                               AS failure_pct
FROM {{ ref('silver_products') }}
GROUP BY domain
HAVING failure_pct > 10.0
ORDER BY failure_pct DESC

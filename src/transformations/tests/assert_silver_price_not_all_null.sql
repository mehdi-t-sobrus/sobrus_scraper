-- tests/assert_silver_price_not_all_null.sql
-- =============================================
-- Singular data test: fails if more than 20% of silver_products rows
-- with extraction_method != 'failed' have a null raw_price.
-- A high null-price rate indicates the price selectors need updating.

SELECT
    domain,
    COUNT(*)                                                    AS total_rows,
    SUM(CASE WHEN raw_price IS NULL THEN 1 ELSE 0 END)         AS null_price_rows,
    ROUND(
        100.0 * SUM(CASE WHEN raw_price IS NULL THEN 1 ELSE 0 END) / COUNT(*),
        2
    )                                                           AS null_price_pct
FROM {{ ref('silver_products') }}
WHERE extraction_method != 'failed'
GROUP BY domain
HAVING null_price_pct > 20.0
ORDER BY null_price_pct DESC

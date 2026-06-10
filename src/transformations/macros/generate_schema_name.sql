-- macros/generate_schema_name.sql
-- =================================
-- Override dbt's default schema naming so models land in predictable
-- schema names regardless of the target profile.
--
-- Default behaviour: dbt appends the target schema to custom schema names,
-- producing "prod_silver" instead of "silver".  This macro removes that
-- prefix so the schema name is always exactly what's declared in the model.

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}


-- macros/export_silver_to_r2.sql
-- ==================================
-- Post-hook macro called after dbt materialises a Silver Python model.
-- In production: exports Parquet to Cloudflare R2.
-- In dev mode (R2_LOCAL_DEV_MODE=True): writes to data/silver/ at project root.

{% macro export_silver_to_r2(model_relation) %}

    {% set run_id        = invocation_id %}
    {% set is_dev        = var('local_dev_mode', 'False') | lower in ('true', '1', 'yes') %}

    {% if is_dev %}

    -- Dev mode: write Parquet to local data/silver/ directory.
    -- DuckDB resolves relative paths from the working directory (project root).
    COPY (
        SELECT * FROM {{ model_relation }}
        WHERE extraction_method != 'failed'
          AND raw_name != ''
    )
    TO 'data/silver/products/'
    (
        FORMAT PARQUET,
        PARTITION_BY (domain, fetched_date),
        OVERWRITE_OR_IGNORE TRUE,
        COMPRESSION 'ZSTD',
        ROW_GROUP_SIZE 100000,
        FILENAME_PATTERN '{{ run_id }}_{i}'
    );

    -- Write manifest locally too
    COPY (
        SELECT
            domain,
            fetched_date,
            COUNT(*)                                                        AS row_count,
            SUM(CASE WHEN raw_price IS NOT NULL THEN 1 ELSE 0 END)         AS priced_count,
            SUM(CASE WHEN raw_ean != '' AND raw_ean IS NOT NULL THEN 1 ELSE 0 END) AS ean_count,
            SUM(CASE WHEN raw_brand != '' AND raw_brand IS NOT NULL THEN 1 ELSE 0 END) AS branded_count,
            COUNT_IF(in_stock = TRUE)                                       AS in_stock_count,
            SUM(CASE WHEN raw_rating IS NOT NULL THEN 1 ELSE 0 END)        AS rated_count,
            COUNT_IF(extraction_method LIKE '%json_ld%')                   AS json_ld_count,
            COUNT_IF(extraction_method LIKE '%og_meta%')                   AS og_meta_count,
            COUNT_IF(extraction_method LIKE '%css_selectors%')             AS css_count,
            MIN(fetched_at_utc)                                             AS earliest_fetch,
            MAX(fetched_at_utc)                                             AS latest_fetch,
            '{{ run_id }}'                                                  AS dbt_run_id,
            current_timestamp                                               AS exported_at
        FROM {{ model_relation }}
        WHERE extraction_method != 'failed'
        GROUP BY domain, fetched_date
    )
    TO 'data/silver/_manifests/{{ run_id }}.parquet'
    (FORMAT PARQUET, COMPRESSION 'ZSTD');

    {% else %}

    -- Production: write Parquet to Cloudflare R2.
    {% set silver_bucket = var('r2_silver_bucket') %}

    COPY (
        SELECT * FROM {{ model_relation }}
        WHERE extraction_method != 'failed'
          AND raw_name != ''
    )
    TO 's3://{{ silver_bucket }}/silver/products/'
    (
        FORMAT PARQUET,
        PARTITION_BY (domain, fetched_date),
        OVERWRITE_OR_IGNORE TRUE,
        COMPRESSION 'ZSTD',
        ROW_GROUP_SIZE 100000,
        FILENAME_PATTERN '{{ run_id }}_{i}'
    );

    COPY (
        SELECT
            domain,
            fetched_date,
            COUNT(*)                                                        AS row_count,
            SUM(CASE WHEN raw_price IS NOT NULL THEN 1 ELSE 0 END)         AS priced_count,
            SUM(CASE WHEN raw_ean != '' AND raw_ean IS NOT NULL THEN 1 ELSE 0 END) AS ean_count,
            SUM(CASE WHEN raw_brand != '' AND raw_brand IS NOT NULL THEN 1 ELSE 0 END) AS branded_count,
            COUNT_IF(in_stock = TRUE)                                       AS in_stock_count,
            SUM(CASE WHEN raw_rating IS NOT NULL THEN 1 ELSE 0 END)        AS rated_count,
            COUNT_IF(extraction_method LIKE '%json_ld%')                   AS json_ld_count,
            COUNT_IF(extraction_method LIKE '%og_meta%')                   AS og_meta_count,
            COUNT_IF(extraction_method LIKE '%css_selectors%')             AS css_count,
            MIN(fetched_at_utc)                                             AS earliest_fetch,
            MAX(fetched_at_utc)                                             AS latest_fetch,
            '{{ run_id }}'                                                  AS dbt_run_id,
            current_timestamp                                               AS exported_at
        FROM {{ model_relation }}
        WHERE extraction_method != 'failed'
        GROUP BY domain, fetched_date
    )
    TO 's3://{{ silver_bucket }}/silver/_manifests/{{ run_id }}.parquet'
    (FORMAT PARQUET, COMPRESSION 'ZSTD');

    {% endif %}

{% endmacro %}
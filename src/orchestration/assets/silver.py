"""
src/orchestration/assets/silver.py
=====================================
Silver layer Dagster asset — dbt transformation.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from dagster import Output, asset, get_dagster_logger

from orchestration.resources.pipeline import PipelineConfig


@asset(group_name="silver", description="Runs dbt-duckdb to transform Bronze HTML into Silver Parquet.", deps=["bronze_scraping"])
def silver_products(pipeline: PipelineConfig) -> Output[dict]:
    log = get_dagster_logger()
    yesterday  = (date.today() - timedelta(days=1)).isoformat()
    today      = date.today().isoformat()
    start_date = os.getenv("DBT_START_DATE", yesterday)
    end_date   = os.getenv("DBT_END_DATE", today)

    log.info("Running dbt silver_products for %s → %s", start_date, end_date)

    try:
        result = pipeline.run_dbt(
            "run", "--select", "silver_products",
            date_vars={"start_date": start_date, "end_date": end_date},
        )
        log.info("dbt output:\n%s", result.stdout[-3000:])
    except Exception as exc:
        log.error("dbt run failed: %s", exc)
        raise

    silver_root   = pipeline.silver_root
    parquet_files = list(silver_root.rglob("*.parquet")) if silver_root.exists() else []
    log.info("Silver complete. %d Parquet files.", len(parquet_files))

    return Output(
        {"start_date": start_date, "end_date": end_date, "parquet_files": len(parquet_files)},
        metadata={"start_date": start_date, "end_date": end_date, "parquet_files": len(parquet_files)},
    )

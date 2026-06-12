"""
src/orchestration/schedules/daily.py
=======================================
Nightly schedule — runs the full Bronze → Silver → Gold pipeline.
"""

from dagster import AssetSelection, ScheduleDefinition, define_asset_job

nightly_pipeline_job = define_asset_job(
    name="nightly_pipeline",
    selection=AssetSelection.all(),
    description="Full Bronze → Silver → Gold pipeline run.",
)

nightly_schedule = ScheduleDefinition(
    job=nightly_pipeline_job,
    cron_schedule="0 2 * * *",   # 2am every night
    name="nightly_pipeline_schedule",
    description="Runs the full scraping and matching pipeline every night at 2am.",
)

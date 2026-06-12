"""
src/orchestration/definitions.py
==================================
Dagster Definitions entry point.

Usage (from repo root):
    export DAGSTER_HOME=$(pwd)/.dagster
    dagster dev -f src/orchestration/definitions.py
"""

from __future__ import annotations

from dagster import Definitions, load_assets_from_modules

from orchestration.assets import bronze, silver, gold
from orchestration.schedules.daily import nightly_schedule, nightly_pipeline_job
from orchestration.resources.pipeline import PipelineConfig

all_assets = load_assets_from_modules([bronze, silver, gold])

defs = Definitions(
    assets=all_assets,
    jobs=[nightly_pipeline_job],
    schedules=[nightly_schedule],
    resources={
        "pipeline": PipelineConfig(),
    },
)

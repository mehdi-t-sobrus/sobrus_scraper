"""
src/orchestration/assets/bronze.py
=====================================
Bronze layer Dagster assets.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dagster import Output, asset, get_dagster_logger

from orchestration.resources.pipeline import PipelineConfig


def _get_url_counts(cfg: PipelineConfig) -> dict:
    result = cfg.run_manage_py(
        "shell", "--command",
        "from scraper_admin.models import ScrapedURL; import json; "
        "print(json.dumps({s: ScrapedURL.objects.filter(status=s).count() "
        "for s in ['pending','in_progress','done','failed','blocked']}))"
    )
    return json.loads(result.stdout.strip())


@asset(group_name="bronze", description="Discovers product URLs for all active sites.")
def bronze_urls(pipeline: PipelineConfig) -> Output[dict]:
    log = get_dagster_logger()

    log.info("Running URL discovery for all active sites...")

    try:
        result = pipeline.run_manage_py("run_discovery")
        summary = json.loads(result.stdout.strip())
        total_found = sum(v.get("found", 0) for v in summary.values())
        log.info("Discovery complete: %s", json.dumps(summary, indent=2))
    except Exception as exc:
        stderr = getattr(exc, 'stderr', '') or ''
        log.error("Discovery failed: %s\nSTDERR: %s", exc, stderr[-2000:])
        summary = {}
        total_found = 0

    counts = _get_url_counts(pipeline)
    total_pending = counts.get("pending", 0)
    log.info("Total pending URLs ready for scraping: %d", total_pending)

    return Output(
        {"sites": summary, "total_urls": total_found, "pending": total_pending},
        metadata={"total_found": total_found, "total_pending": total_pending},
    )


@asset(group_name="bronze", description="Monitors Arq worker until all URLs are scraped.", deps=[bronze_urls])
def bronze_scraping(pipeline: PipelineConfig) -> Output[dict]:
    log = get_dagster_logger()
    timeout_hours = float(os.getenv("SCRAPING_TIMEOUT_HOURS", "6"))
    poll_seconds  = int(os.getenv("SCRAPING_POLL_SECONDS", "60"))
    deadline      = time.time() + timeout_hours * 3600

    log.info("Monitoring scraping (timeout=%.1fh, poll=%ds)...", timeout_hours, poll_seconds)

    while time.time() < deadline:
        counts      = _get_url_counts(pipeline)
        pending     = counts.get("pending", 0)
        in_progress = counts.get("in_progress", 0)
        done        = counts.get("done", 0)
        failed      = counts.get("failed", 0)
        blocked     = counts.get("blocked", 0)

        log.info(
            "pending=%d | in_progress=%d | done=%d | failed=%d | blocked=%d",
            pending, in_progress, done, failed, blocked,
        )

        if pending == 0 and in_progress == 0:
            log.info("Scraping complete.")
            return Output(
                {"done": done, "failed": failed, "blocked": blocked,
                 "completed_at": datetime.now(timezone.utc).isoformat()},
                metadata={"urls_done": done, "urls_failed": failed, "urls_blocked": blocked},
            )

        time.sleep(poll_seconds)

    log.warning("Scraping timed out after %.1f hours.", timeout_hours)
    return Output({"status": "timeout"}, metadata={"status": "timeout"})
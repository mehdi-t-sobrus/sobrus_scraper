"""
src/orchestration/assets/gold.py
===================================
Gold layer Dagster asset — entity resolution.
"""

from __future__ import annotations

import json

from dagster import Output, asset, get_dagster_logger

from orchestration.resources.pipeline import PipelineConfig


def _get_active_sites(cfg: PipelineConfig) -> list[str]:
    result = cfg.run_manage_py(
        "shell", "--command",
        "from scraper_admin.models import SiteConfig; import json; "
        "print(json.dumps(list(SiteConfig.objects.filter(status='active')"
        ".order_by('name').values_list('domain', flat=True))))"
    )
    return json.loads(result.stdout.strip())


@asset(group_name="gold", description="Runs entity resolution to match Silver products to MasterProducts.", deps=["silver_products"])
def gold_matching(pipeline: PipelineConfig) -> Output[dict]:
    log = get_dagster_logger()
    active_sites = _get_active_sites(pipeline)

    if not active_sites:
        log.warning("No active sites found.")
        return Output({"sites": {}, "total_masters": 0})

    results = {}
    total_new_masters = 0
    total_price_logs  = 0
    total_flagged     = 0

    for domain in active_sites:
        log.info("Running Gold matching for %s...", domain)
        try:
            result = pipeline.run_manage_py("run_matching", "--site", domain)
            stdout = result.stdout.strip()
            log.info("[%s] %s", domain, stdout)

            for line in stdout.splitlines():
                if "new MasterProducts" in line:
                    try:
                        parts = line.replace("Done.", "").strip().split(",")
                        total_new_masters += int(parts[0].strip().split()[0])
                        total_flagged     += int(parts[1].strip().split()[0])
                        total_price_logs  += int(parts[2].strip().split()[0])
                    except (ValueError, IndexError):
                        pass

            results[domain] = "ok"
        except Exception as exc:
            stderr = getattr(exc, 'stderr', '') or ''
            log.error("[%s] Matching failed: %s\nSTDERR: %s", domain, exc, stderr[-3000:])
            results[domain] = "failed"

    log.info(
        "Gold complete. new_masters=%d, price_logs=%d, flagged=%d",
        total_new_masters, total_price_logs, total_flagged,
    )

    return Output(
        {"sites": results, "new_masters": total_new_masters,
         "price_logs": total_price_logs, "flagged": total_flagged},
        metadata={
            "new_master_products": total_new_masters,
            "price_logs_written":  total_price_logs,
            "flagged_for_review":  total_flagged,
        },
    )

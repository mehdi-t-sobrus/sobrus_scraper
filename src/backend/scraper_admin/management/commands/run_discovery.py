"""
src/backend/scraper_admin/management/commands/run_discovery.py
===============================================================
Django management command for running URL discovery.
Called by the Dagster bronze_urls asset via subprocess.

Usage:
    python manage.py run_discovery
    python manage.py run_discovery --sites beautymarket.ma universparadiscount.ma
    python manage.py run_discovery --no-enqueue
    python manage.py run_discovery --rescrape
"""

import asyncio
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run URL discovery for all active sites or a subset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sites",
            nargs="+",
            default=None,
            help="Specific site domains to discover. Defaults to all active sites.",
        )
        parser.add_argument(
            "--no-enqueue",
            action="store_true",
            default=False,
            help="Discover URLs without enqueueing Arq jobs.",
        )
        parser.add_argument(
            "--rescrape",
            action="store_true",
            default=False,
            help="Reset done/failed URLs back to pending for re-scraping.",
        )

    def handle(self, *args, **options):
        from scrapers.discoverer import run_discovery

        results = asyncio.run(run_discovery(
            site_domains=options["sites"],
            enqueue=not options["no_enqueue"],
            rescrape=options["rescrape"],
        ))

        # Output clean JSON for the Dagster asset to parse
        summary = {}
        for site_result in results:
            domain = site_result.get("site", "unknown")
            summary[domain] = {
                "found":   site_result.get("urls_found", 0),
                "new":     site_result.get("urls_created", 0),
                "jobs":    site_result.get("jobs_enqueued", 0),
                "status":  "ok" if not site_result.get("error") else "failed",
            }

        self.stdout.write(json.dumps(summary))

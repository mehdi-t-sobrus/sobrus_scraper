"""
src/backend/products/management/commands/run_matching.py
=========================================================
Django management command wrapper for the entity resolution engine.

Usage
-----
    python manage.py run_matching
    python manage.py run_matching --site universparadiscount.ma
    python manage.py run_matching --date 2026-06-10
    python manage.py run_matching --site beautymarket.ma --date 2026-06-10
    python manage.py run_matching --dry-run
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run Gold-layer entity resolution — matches Silver products to MasterProduct catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            type=str,
            default=None,
            help="Only process Silver records for this domain (e.g. universparadiscount.ma).",
        )
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Only process Silver records from this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Run matching without writing to the database. Useful for tuning thresholds.",
        )

    def handle(self, *args, **options):
        # Import here to avoid circular imports at Django startup
        from matching.entity_res import run_matching

        self.stdout.write(
            self.style.NOTICE(
                f"Starting entity resolution "
                f"(site={options['site'] or 'all'}, "
                f"date={options['date'] or 'all'}, "
                f"dry_run={options['dry_run']})"
            )
        )

        stats = run_matching(
            site_domain=options["site"],
            target_date=options["date"],
            dry_run=options["dry_run"],
        )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes made."))

        self.stdout.write(self.style.SUCCESS(
            f"Done. "
            f"{stats.get('new_masters', 0)} new MasterProducts, "
            f"{stats.get('flagged_review', 0)} flagged for review, "
            f"{stats.get('price_logs', 0)} price logs written."
        ))

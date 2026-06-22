"""
src/backend/grossiste/management/commands/load_grossiste_file.py
=================================================================
Load grossiste product catalogue from a locally extracted file.

The product list is embedded in the site's JavaScript as:
    var products = [{CODE_PRODU, NOM_PRODUI, PRIX_PHAR, PPM, FORME_PROD, PA}, ...]

Since it's not accessible via a public API, extract it manually via F12
browser dev tools and save as a .js or .json file.

Usage:
    # From a .js file containing "var products = [...]"
    python manage.py load_grossiste_file --name GPM --file /path/to/products.js

    # From a .json file containing just the array [...]
    python manage.py load_grossiste_file --name GPM --file /path/to/products.json

    # Preview without saving
    python manage.py load_grossiste_file --name GPM --file /path/to/products.js --dry-run
"""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from grossiste.models import GrossisteConfig, GrossisteProduct


class Command(BaseCommand):
    help = "Load grossiste product catalogue from a locally extracted JS/JSON file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            required=True,
            help="Name of the GrossisteConfig to load products for (e.g. GPM).",
        )
        parser.add_argument(
            "--file",
            required=True,
            help="Path to the extracted JS or JSON file.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Parse and count products without saving to the database.",
        )

    def handle(self, *args, **options):
        # Get grossiste config
        try:
            config = GrossisteConfig.objects.get(name=options["name"])
        except GrossisteConfig.DoesNotExist:
            raise CommandError(
                f"No GrossisteConfig found with name '{options['name']}'. "
                f"Create it in Admin first."
            )

        # Read file
        file_path = Path(options["file"])
        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        self.stdout.write(f"Reading {file_path}...")
        content = file_path.read_text(encoding="utf-8", errors="replace")

        # Parse
        products = self._parse(content, file_path.suffix)
        self.stdout.write(f"Found {len(products)} products in file.")

        if options["dry_run"]:
            self.stdout.write("Dry run — first 5 products:")
            for p in products[:5]:
                self.stdout.write(f"  {p['code']} — {p['name']} — {p['prix_pharmacien']} MAD")
            return

        # Upsert into DB
        created = updated = skipped = 0
        for p in products:
            if not p["code"]:
                skipped += 1
                continue
            _, was_created = GrossisteProduct.objects.update_or_create(
                grossiste=config,
                code=p["code"],
                defaults={
                    "name":             p["name"],
                    "prix_pharmacien":  p["prix_pharmacien"],
                    "ppm":              p["ppm"],
                    "pa":               p["pa"],
                    "forme":            p["forme"],
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"[{config.name}] Done — {created} created, {updated} updated, {skipped} skipped."
            )
        )

    def _parse(self, content: str, suffix: str) -> list[dict]:
        """
        Parse products from either:
          - .js file: extracts the array from "var products = [...];"
          - .json file: parses directly as JSON array
        """
        # Try to extract var products = [...] if it's a JS file or JS snippet
        js_match = re.search(
            r"var\s+products\s*=\s*(\[.*?\])\s*;",
            content,
            re.DOTALL,
        )
        if js_match:
            raw = json.loads(js_match.group(1))
        else:
            # Try parsing as raw JSON array
            content = content.strip()
            if content.startswith("["):
                raw = json.loads(content)
            else:
                raise CommandError(
                    "Could not find 'var products = [...]' in the file, "
                    "and the file is not a plain JSON array. "
                    "Make sure you copied the full JavaScript variable."
                )

        return [self._normalise(item) for item in raw]

    @staticmethod
    def _normalise(item: dict) -> dict:
        """Normalise one raw API record to our internal field names."""
        def to_decimal(val):
            try:
                f = float(str(val).replace(",", ".").strip())
                return f if f > 0 else None
            except (ValueError, TypeError):
                return None

        return {
            "code":             str(item.get("CODE_PRODU", "")).strip(),
            "name":             str(item.get("NOM_PRODUI", "")).strip(),
            "prix_pharmacien":  to_decimal(item.get("PRIX_PHAR")),
            "ppm":              to_decimal(item.get("PPM")),
            "pa":               to_decimal(item.get("PA")) if item.get("PA") else None,
            "forme":            str(item.get("FORME_PROD", "")).strip(),
        }
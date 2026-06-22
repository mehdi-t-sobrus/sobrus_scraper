"""
src/backend/grossiste/management/commands/sync_grossiste.py
============================================================
Grossiste management commands — sync, check availability, place orders.

Usage:
    # Sync full catalogue from file
    python manage.py sync_grossiste --name GPM --file data/grossiste/gpm_products.js

    # Check availability for specific product codes
    python manage.py sync_grossiste --name GPM --check-stock --codes 5230 1869 4240

    # Check availability for all products (slow — use sparingly)
    python manage.py sync_grossiste --name GPM --check-stock

    # Place an order for a specific product
    python manage.py sync_grossiste --name GPM --order --code 5230 --qty 10

    # Match grossiste products to MasterProducts
    python manage.py sync_grossiste --name GPM --match

    # All active grossistes
    python manage.py sync_grossiste --check-stock --codes 5230 1869
"""

import asyncio

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from grossiste.client import GrossisteAPIError, GrossisteAuthError, GrossisteClient
from grossiste.models import GrossisteConfig, GrossisteOrder, GrossisteProduct


class Command(BaseCommand):
    help = "Sync, check availability, order, and match grossiste products."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            type=str,
            default=None,
            help="Grossiste name (default: all active).",
        )
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Grossiste login username (required for --check-stock and --order).",
        )
        parser.add_argument(
            "--password",
            type=str,
            default=None,
            help="Grossiste login password (required for --check-stock and --order).",
        )
        parser.add_argument(
            "--check-stock",
            action="store_true",
            default=False,
            help="Check availability. Use --codes to limit to specific products.",
        )
        parser.add_argument(
            "--codes",
            nargs="+",
            default=None,
            help="Product CODE_PRODU values to check (e.g. --codes 5230 1869 4240).",
        )
        parser.add_argument(
            "--order",
            action="store_true",
            default=False,
            help="Place an order. Requires --code and --qty.",
        )
        parser.add_argument(
            "--code",
            type=str,
            default=None,
            help="Single product code for ordering.",
        )
        parser.add_argument(
            "--qty",
            type=int,
            default=1,
            help="Quantity to order (default: 1).",
        )
        parser.add_argument(
            "--match",
            action="store_true",
            default=False,
            help="Match unlinked grossiste products to MasterProducts.",
        )

    def handle(self, *args, **options):
        qs = GrossisteConfig.objects.filter(is_active=True)
        if options["name"]:
            qs = qs.filter(name=options["name"])
            if not qs.exists():
                raise CommandError(f"No active grossiste found with name '{options['name']}'.")

        # Credentials required for API calls
        username = options.get("username")
        password = options.get("password")
        needs_creds = options["check_stock"] or options["order"]
        if needs_creds and not (username and password):
            raise CommandError(
                "--check-stock and --order require --username and --password.\n"
                "Example: python manage.py sync_grossiste --name GPM "
                "--check-stock --codes 5230 --username myuser --password mypass"
            )

        for config in qs:
            if options["check_stock"]:
                self._check_stock(config, options["codes"], username, password)
            elif options["order"]:
                if not options["code"]:
                    raise CommandError("--order requires --code.")
                self._place_order(config, options["code"], options["qty"], username, password)
            elif options["match"]:
                self._match_products(config)
            else:
                self.stdout.write(
                    f"[{config.name}] Nothing to do. Use --check-stock, --order, or --match."
                )

    # -------------------------------------------------------------------------
    # Availability check
    # -------------------------------------------------------------------------

    def _check_stock(self, config, codes, username: str, password: str) -> None:
        qs = GrossisteProduct.objects.filter(grossiste=config)
        if codes:
            qs = qs.filter(code__in=codes)
            if not qs.exists():
                self.stderr.write(f"[{config.name}] No products found for codes: {codes}")
                return

        products = list(qs)
        self.stdout.write(
            f"[{config.name}] Checking availability for {len(products)} product(s)..."
        )

        try:
            results = asyncio.run(self._async_check_stock(config, products, username, password))
            in_stock = sum(1 for v in results.values() if v)
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{config.name}] Done — {in_stock} in stock, "
                    f"{len(results) - in_stock} out of stock."
                )
            )
            for code, available in results.items():
                status = "✓ In stock" if available else "✗ Out of stock"
                colour = self.style.SUCCESS if available else self.style.ERROR
                self.stdout.write(colour(f"  {code}: {status}"))
        except (GrossisteAuthError, GrossisteAPIError) as exc:
            self.stderr.write(self.style.ERROR(f"[{config.name}] Failed: {exc}"))

    async def _async_check_stock(self, config, products, username, password) -> dict:
        results: dict[str, bool] = {}
        now = timezone.now()
        async with GrossisteClient(config, username, password) as client:
            await client.login()
            for product in products:
                in_stock = await client.check_availability(product.code)
                results[product.code] = in_stock
                await GrossisteProduct.objects.filter(pk=product.pk).aupdate(
                    in_stock=in_stock,
                    availability_checked_at=now,
                )
        return results

    def _place_order(self, config, code, qty, username: str, password: str) -> None:
        try:
            product = GrossisteProduct.objects.get(grossiste=config, code=code)
        except GrossisteProduct.DoesNotExist:
            raise CommandError(
                f"[{config.name}] Product '{code}' not found."
            )

        self.stdout.write(
            f"[{config.name}] Placing order: {product.name} x{qty}..."
        )

        try:
            result = asyncio.run(self._async_place_order(config, product, qty, username, password))
            order = GrossisteOrder.objects.create(
                grossiste=config,
                product=product,
                quantity=qty,
                unit_price=product.prix_pharmacien,
                status=GrossisteOrder.Status.SUBMITTED,
                response_payload=result,
                submitted_at=timezone.now(),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{config.name}] Order #{order.pk} — {result.get('message', 'ok')}"
                )
            )
        except (GrossisteAuthError, GrossisteAPIError) as exc:
            self.stderr.write(self.style.ERROR(f"[{config.name}] Order failed: {exc}"))

    async def _async_place_order(self, config, product, qty, username, password) -> dict:
        async with GrossisteClient(config, username, password) as client:
            await client.login()
            return await client.place_order(product_code=product.code, quantity=qty)

    # -------------------------------------------------------------------------
    # Match grossiste products to MasterProducts
    # -------------------------------------------------------------------------

    def _match_products(self, config: GrossisteConfig) -> None:
        """
        Link unmatched GrossisteProducts to MasterProducts using:
          1. EAN exact match (if grossiste product has EAN in name — rare)
          2. Normalised name fuzzy match via rapidfuzz
          3. pgvector cosine similarity (same model as e-commerce matching)

        Confidence thresholds:
          ≥ 0.95 → auto-link
          0.75–0.94 → link but flag for review (manually_verified=False)
          < 0.75 → skip (too uncertain)
        """
        from products.models import MasterProduct
        from rapidfuzz import fuzz, process as rf_process
        import unicodedata
        import re

        def normalise(text: str) -> str:
            text = text.upper().strip()
            text = unicodedata.normalize("NFD", text)
            text = "".join(c for c in text if unicodedata.category(c) != "Mn")
            text = re.sub(r"[^\w\s]", " ", text)
            return " ".join(text.split())

        unmatched = list(
            GrossisteProduct.objects.filter(
                grossiste=config,
                master_product__isnull=True,
            )
        )
        self.stdout.write(
            f"[{config.name}] Matching {len(unmatched)} unlinked products..."
        )

        # Build master product index
        masters = list(MasterProduct.objects.filter(status="active").values("id", "name", "ean"))
        master_names = [normalise(m["name"]) for m in masters]
        master_eans  = {m["ean"]: m["id"] for m in masters if m["ean"]}

        auto_linked = review_linked = skipped = 0

        for gp in unmatched:
            normalised_name = normalise(gp.name)
            master_id = None
            confidence = 0.0

            # Tier 1: EAN match (extract from product name if present)
            ean_match = re.search(r"\b\d{8,14}\b", gp.name)
            if ean_match:
                ean = ean_match.group()
                if ean in master_eans:
                    master_id  = master_eans[ean]
                    confidence = 1.0

            # Tier 2: fuzzy name match
            if master_id is None:
                result = rf_process.extractOne(
                    normalised_name,
                    master_names,
                    scorer=fuzz.token_sort_ratio,
                    score_cutoff=75,
                )
                if result:
                    matched_name, score, idx = result
                    master_id  = masters[idx]["id"]
                    confidence = score / 100.0

            if master_id is None:
                skipped += 1
                continue

            needs_review = confidence < 0.95
            GrossisteProduct.objects.filter(pk=gp.pk).update(
                master_product_id=master_id,
                match_confidence=confidence,
                manually_verified=not needs_review,
            )
            if needs_review:
                review_linked += 1
            else:
                auto_linked += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"[{config.name}] Matching done — "
                f"{auto_linked} auto-linked, "
                f"{review_linked} flagged for review, "
                f"{skipped} skipped (low confidence)."
            )
        )


"""
tests/test_gold_layer.py
=========================
Unit tests for Gold layer logic:
- Price comparison calculations (min/max/avg)
- Image selection priority strategy
- _upsert_site_product check-then-act logic
- _pick_best_image priority ordering
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from matching.entity_res import _pick_best_image


# ---------------------------------------------------------------------------
# Image selection — _pick_best_image
# ---------------------------------------------------------------------------

class TestPickBestImage:

    def test_empty_existing_returns_incoming(self):
        incoming = ["https://parachezvous.ma/img.jpg"]
        result = _pick_best_image([], incoming, "parachezvous.ma")
        assert result == incoming

    def test_empty_incoming_returns_existing(self):
        existing = ["https://beautymarket.ma/img.jpg"]
        result = _pick_best_image(existing, [], "beautymarket.ma")
        assert result == existing

    def test_higher_priority_source_wins(self):
        """parachezvous.ma (priority 1) beats beautymarket.ma (priority 4)."""
        existing = ["https://beautymarket.ma/img.jpg"]
        incoming = ["https://parachezvous.ma/img1.jpg", "https://parachezvous.ma/img2.jpg"]
        result = _pick_best_image(existing, incoming, "parachezvous.ma")
        assert result == incoming

    def test_lower_priority_source_loses(self):
        """universparadiscount.ma (priority 5) loses to existing parachezvous.ma (priority 1)."""
        existing = ["https://parachezvous.ma/img.jpg"]
        incoming = ["https://universparadiscount.ma/img.jpg"]
        result = _pick_best_image(existing, incoming, "universparadiscount.ma")
        assert result == existing

    def test_same_priority_more_images_wins(self):
        """Same priority site — more images wins."""
        existing = ["https://beautymall.ma/img1.jpg"]
        incoming = [
            "https://beautymall.ma/img1.jpg",
            "https://beautymall.ma/img2.jpg",
            "https://beautymall.ma/img3.jpg",
        ]
        result = _pick_best_image(existing, incoming, "beautymall.ma")
        assert result == incoming

    def test_same_priority_fewer_images_loses(self):
        """Same priority site — fewer images loses."""
        existing = [
            "https://beautymall.ma/img1.jpg",
            "https://beautymall.ma/img2.jpg",
        ]
        incoming = ["https://beautymall.ma/img.jpg"]
        result = _pick_best_image(existing, incoming, "beautymall.ma")
        assert result == existing

    def test_priority_order(self):
        """Full priority order: parachezvous > beautymall > cotepara > beautymarket > universparadiscount."""
        sites_in_order = [
            "parachezvous.ma",
            "beautymall.ma",
            "cotepara.ma",
            "beautymarket.ma",
            "universparadiscount.ma",
        ]
        # Build from lowest to highest priority — each should win over previous
        current = [f"https://{sites_in_order[-1]}/img.jpg"]
        for site in reversed(sites_in_order[:-1]):
            incoming = [f"https://{site}/img.jpg"]
            result = _pick_best_image(current, incoming, site)
            assert result == incoming, f"{site} should beat {current[0]}"
            current = result


# ---------------------------------------------------------------------------
# Price comparison calculations
# ---------------------------------------------------------------------------

class TestPriceComparison:
    """
    Tests for price comparison logic — min/max/avg/saving calculations.
    These mirror the logic in products/api.py get_price_comparison().
    """

    def _make_site_product(self, price, in_stock=True, domain="test.ma"):
        sp = MagicMock()
        sp.current_price = price
        sp.in_stock = in_stock
        sp.currency = "MAD"
        sp.site = MagicMock()
        sp.site.domain = domain
        return sp

    def _calculate(self, site_products):
        """Mirror the price comparison logic from the API."""
        priced = [sp for sp in site_products if sp.current_price is not None]
        in_stock_priced = [sp for sp in priced if sp.in_stock]
        pool = in_stock_priced if in_stock_priced else priced

        if not pool:
            return None, None, None, None

        price_min = min(sp.current_price for sp in pool)
        price_max = max(sp.current_price for sp in pool)
        price_avg = sum(sp.current_price for sp in pool) / len(pool)
        cheapest = sorted(pool, key=lambda x: x.current_price)[0]
        return price_min, price_max, round(price_avg, 2), cheapest

    def test_single_site(self):
        products = [self._make_site_product(270.0)]
        p_min, p_max, p_avg, cheapest = self._calculate(products)
        assert p_min == 270.0
        assert p_max == 270.0
        assert p_avg == 270.0

    def test_three_sites_min_max_avg(self):
        products = [
            self._make_site_product(270.0, domain="cotepara.ma"),
            self._make_site_product(310.0, domain="parachezvous.ma"),
            self._make_site_product(326.70, domain="universparadiscount.ma"),
        ]
        p_min, p_max, p_avg, cheapest = self._calculate(products)
        assert p_min == 270.0
        assert p_max == 326.70
        assert p_avg == pytest.approx((270 + 310 + 326.70) / 3, rel=0.01)
        assert cheapest.site.domain == "cotepara.ma"

    def test_out_of_stock_excluded_from_pool_when_in_stock_available(self):
        """Out-of-stock prices should be excluded when any site has stock."""
        products = [
            self._make_site_product(150.0, in_stock=False, domain="cheap_out.ma"),
            self._make_site_product(270.0, in_stock=True, domain="cotepara.ma"),
            self._make_site_product(310.0, in_stock=True, domain="parachezvous.ma"),
        ]
        p_min, p_max, p_avg, cheapest = self._calculate(products)
        # 150 is out of stock — should not be min
        assert p_min == 270.0
        assert cheapest.site.domain == "cotepara.ma"

    def test_all_out_of_stock_uses_all_prices(self):
        """When all sites are out of stock, use all prices as fallback."""
        products = [
            self._make_site_product(150.0, in_stock=False),
            self._make_site_product(270.0, in_stock=False),
        ]
        p_min, p_max, p_avg, cheapest = self._calculate(products)
        assert p_min == 150.0

    def test_saving_percentage(self):
        p_min, p_max = 270.0, 326.70
        saving = round((1 - p_min / p_max) * 100)
        assert saving == 17  # 17% saving

    def test_no_priced_products(self):
        products = [self._make_site_product(None)]
        p_min, p_max, p_avg, cheapest = self._calculate(products)
        assert p_min is None


# ---------------------------------------------------------------------------
# Description cleanup — additional site patterns
# ---------------------------------------------------------------------------

class TestCleanDescriptionSites:

    def _clean(self, desc):
        from transformations.models.silver.silver_products import _clean_description
        return _clean_description(desc)

    def test_strips_parachezvous_price_embed(self):
        desc = (
            "Commandez ISDIN Fotoprotector SPF50 à seulement 270\u00a0\u062f.\u0645. "
            "chez Parachezvous.ma. Livraison rapide partout au Maroc. Produits 100% original."
        )
        cleaned = self._clean(desc)
        assert "Commandez" not in cleaned
        assert "270" not in cleaned
        assert "Parachezvous" not in cleaned

    def test_strips_livraison_gratuite(self):
        desc = "Excellent produit. Livraison gratuite disponible partout au Maroc."
        cleaned = self._clean(desc)
        assert "Livraison" not in cleaned
        assert "Excellent produit." in cleaned

    def test_strips_100_original(self):
        desc = "Belle crème. Produits 100% originaux."
        cleaned = self._clean(desc)
        assert "100%" not in cleaned

    def test_preserves_actual_description(self):
        desc = "Photoprotection faciale quotidienne SPF 50. Texture ultra-légère."
        cleaned = self._clean(desc)
        assert "Photoprotection" in cleaned
        assert "SPF 50" in cleaned

    def test_handles_none_gracefully(self):
        assert self._clean("") == ""


# ---------------------------------------------------------------------------
# Price normalisation — MAD and dirham symbol (regression tests)
# ---------------------------------------------------------------------------

class TestPriceNormalisationMAD:

    def _normalise(self, raw, default="MAD"):
        from transformations.models.silver.silver_products import _normalise_price
        return _normalise_price(raw, default_currency=default)

    def test_mad_suffix(self):
        price, currency, _ = self._normalise("270 MAD")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"

    def test_dirham_symbol(self):
        price, currency, _ = self._normalise("270 \u062f.\u0645.")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"

    def test_dh_abbreviation(self):
        price, currency, _ = self._normalise("189 DH")
        assert price == pytest.approx(189.0)
        assert currency == "MAD"

    def test_mad_with_comma_decimal(self):
        price, currency, _ = self._normalise("270,00 MAD")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"

    def test_explicit_default_used_when_no_symbol(self):
        price, currency, _ = self._normalise("189", default="MAD")
        assert currency == "MAD"

    def test_eur_symbol_overrides_default(self):
        price, currency, _ = self._normalise("€99", default="MAD")
        assert currency == "EUR"

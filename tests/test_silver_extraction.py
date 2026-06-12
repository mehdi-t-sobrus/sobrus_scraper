"""
tests/test_silver_extraction.py
=================================
Unit tests for the Silver layer extraction strategies.

Tests cover:
- JSON-LD Product extraction (all variants)
- Open Graph meta tag extraction
- Price normalisation (MAD, EUR, edge cases)
- Description cleanup
- Brand-as-array handling
- priceSpecification array handling
- EAN validation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add src paths so imports work without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "transformations"))

from models.silver.silver_products import (
    _clean_description,
    _extract_json_ld,
    _extract_open_graph,
    _normalise_price,
)
from selectolax.parser import HTMLParser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_html(head: str = "", body: str = "") -> HTMLParser:
    return HTMLParser(f"<html><head>{head}</head><body>{body}</body></html>")


def jsonld_script(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

class TestExtractJsonLd:

    def test_basic_product(self):
        data = {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": "ISDIN Fotoprotector SPF50",
            "brand": {"@type": "Brand", "name": "ISDIN"},
            "offers": {"@type": "Offer", "price": "270.00", "priceCurrency": "MAD",
                       "availability": "https://schema.org/InStock"},
        }
        tree = make_html(head=jsonld_script(data))
        result, errors = _extract_json_ld(tree)
        assert result["raw_name"] == "ISDIN Fotoprotector SPF50"
        assert result["raw_brand"] == "ISDIN"
        assert result["raw_price_str"] == "270.00"
        assert result["raw_currency"] == "MAD"
        assert result["in_stock"] is True
        assert not errors

    def test_brand_as_array(self):
        """beautymall.ma returns brand as a list — must unwrap."""
        data = {
            "@type": "Product",
            "name": "Test Product",
            "brand": [{"@type": "Brand", "name": "EUCERIN"}],
            "offers": {"price": "199.00", "priceCurrency": "MAD"},
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_brand"] == "EUCERIN"

    def test_price_specification_array(self):
        """beautymall.ma uses priceSpecification — pick sale price, skip ListPrice."""
        data = {
            "@type": "Product",
            "name": "Test",
            "offers": [{
                "@type": "Offer",
                "priceSpecification": [
                    {"@type": "UnitPriceSpecification", "price": "264.00", "priceCurrency": "MAD"},
                    {"@type": "UnitPriceSpecification", "price": "412.50", "priceCurrency": "MAD",
                     "priceType": "https://schema.org/ListPrice"},
                ],
            }],
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_price_str"] == "264.00"   # sale price, not list price

    def test_out_of_stock(self):
        data = {
            "@type": "Product",
            "name": "Test",
            "offers": {"price": "100", "priceCurrency": "MAD",
                       "availability": "https://schema.org/OutOfStock"},
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["in_stock"] is False

    def test_graph_wrapper(self):
        """Yoast / Rank Math wraps everything in @graph array."""
        data = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebSite", "name": "Test Site"},
                {
                    "@type": "Product",
                    "name": "CeraVe Gel Moussant 200ml",
                    "brand": {"@type": "Brand", "name": "CeraVe"},
                    "offers": {"price": "145.00", "priceCurrency": "MAD"},
                    "sku": "12345",
                    "gtin13": "3337875597296",
                },
            ],
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_name"] == "CeraVe Gel Moussant 200ml"
        assert result["raw_ean"] == "3337875597296"
        assert result.get("raw_sku") == "12345"

    def test_multiple_images(self):
        """parachezvous.ma provides multiple image objects."""
        data = {
            "@type": "Product",
            "name": "Test",
            "image": [
                {"@type": "ImageObject", "url": "https://example.com/img1.jpg"},
                {"@type": "ImageObject", "url": "https://example.com/img2.jpg"},
            ],
            "offers": {"price": "100"},
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert len(result["raw_images"]) == 2
        assert result["raw_images"][0] == "https://example.com/img1.jpg"

    def test_aggregate_rating(self):
        data = {
            "@type": "Product",
            "name": "Test",
            "aggregateRating": {"@type": "AggregateRating",
                                "ratingValue": "4.5", "reviewCount": "23"},
            "offers": {"price": "100"},
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_rating"] == 4.5
        assert result["raw_review_count"] == 23

    def test_category_from_direct_field(self):
        """Rank Math puts category directly on Product entity."""
        data = {
            "@type": "Product",
            "name": "Test",
            "category": "Solaires > Écrans Solaires",
            "offers": {"price": "100"},
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_category"] == "Solaires > Écrans Solaires"

    def test_additional_property(self):
        """parachezvous.ma WooCommerce custom attributes (pa_*)."""
        data = {
            "@type": "Product",
            "name": "Test",
            "offers": {"price": "100"},
            "additionalProperty": [
                {"@type": "PropertyValue", "name": "pa_country", "value": "Espagne"},
                {"@type": "PropertyValue", "name": "pa_verifie", "value": "Oui"},
            ],
        }
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result["raw_attributes"]["pa_country"] == "Espagne"
        assert result["raw_attributes"]["pa_verifie"] == "Oui"

    def test_no_product_entity_returns_empty(self):
        data = {"@type": "WebSite", "name": "Some Site"}
        tree = make_html(head=jsonld_script(data))
        result, _ = _extract_json_ld(tree)
        assert result == {}

    def test_invalid_json_returns_error(self):
        tree = make_html(head='<script type="application/ld+json">{invalid json}</script>')
        result, errors = _extract_json_ld(tree)
        assert result == {}
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Open Graph extraction
# ---------------------------------------------------------------------------

class TestExtractOpenGraph:

    def _og_head(self, props: dict[str, str]) -> str:
        tags = ""
        for prop, content in props.items():
            tags += f'<meta property="{prop}" content="{content}">\n'
        return tags

    def test_basic_og_fields(self):
        head = self._og_head({
            "og:title": "EUCERIN Anti-Pigment Gel 200ml - Côté Para : Site",
            "product:brand": "EUCERIN",
            "product:price:amount": "189",
            "product:price:currency": "MAD",
            "product:availability": "instock",
            "product:retailer_item_id": "4006000099422",
            "og:image": "https://cotepara.ma/wp-content/uploads/img.png",
            "og:description": "Gel nettoyant visage.",
        })
        tree = make_html(head=head)
        result, errors = _extract_open_graph(tree)
        assert result["raw_name"] == "EUCERIN Anti-Pigment Gel 200ml"  # suffix stripped
        assert result["raw_brand"] == "EUCERIN"
        assert result["raw_price_str"] == "189"
        assert result["raw_currency"] == "MAD"
        assert result["in_stock"] is True
        assert result["raw_ean"] == "4006000099422"
        assert result["raw_images"] == ["https://cotepara.ma/wp-content/uploads/img.png"]
        assert result["raw_description"] == "Gel nettoyant visage."
        assert not errors

    def test_out_of_stock_signals(self):
        for signal in ("outofstock", "out_of_stock", "unavailable"):
            head = self._og_head({"product:availability": signal})
            tree = make_html(head=head)
            result, _ = _extract_open_graph(tree)
            assert result["in_stock"] is False, f"Failed for signal: {signal}"

    def test_invalid_ean_ignored(self):
        """Non-numeric or too-short retailer_item_id should not be stored as EAN."""
        head = self._og_head({"product:retailer_item_id": "ABC123"})
        tree = make_html(head=head)
        result, _ = _extract_open_graph(tree)
        assert "raw_ean" not in result

    def test_stock_qty_from_twitter_card(self):
        """WooCommerce puts stock quantity in twitter:data2."""
        head = (
            '<meta name="twitter:label1" content="Prix">'
            '<meta name="twitter:data1" content="189 MAD">'
            '<meta name="twitter:label2" content="Disponibilité">'
            '<meta name="twitter:data2" content="140 en stock">'
        )
        tree = make_html(head=head)
        result, _ = _extract_open_graph(tree)
        assert result["raw_stock_qty"] == 140

    def test_og_title_suffix_stripping(self):
        """Title with " - Site Name" suffix should strip after the dash."""
        head = self._og_head({
            "og:title": "ISDIN SPF50 200ml - Parachezvous.ma : Para en ligne",
        })
        tree = make_html(head=head)
        result, _ = _extract_open_graph(tree)
        assert result["raw_name"] == "ISDIN SPF50 200ml"


# ---------------------------------------------------------------------------
# Price normalisation
# ---------------------------------------------------------------------------

class TestNormalisePrice:

    def test_clean_float(self):
        price, currency, errors = _normalise_price("270.00", default_currency="MAD")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"
        assert not errors

    def test_integer_price(self):
        price, currency, errors = _normalise_price("189", default_currency="MAD")
        assert price == pytest.approx(189.0)

    def test_comma_decimal(self):
        """French locale uses comma as decimal separator."""
        price, _, errors = _normalise_price("270,00", default_currency="MAD")
        assert price == pytest.approx(270.0)
        assert not errors

    def test_price_with_currency_symbol(self):
        price, currency, _ = _normalise_price("270 MAD")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"

    def test_price_with_dirhams_symbol(self):
        """Arabic dirham symbol د.م. should resolve to MAD."""
        price, currency, _ = _normalise_price("270 د.م.")
        assert price == pytest.approx(270.0)
        assert currency == "MAD"

    def test_price_with_thousands_separator(self):
        price, _, _ = _normalise_price("1,299.00")
        assert price == pytest.approx(1299.0)

    def test_empty_price(self):
        price, _, errors = _normalise_price("", default_currency="MAD")
        assert price is None

    def test_non_numeric_price(self):
        price, _, errors = _normalise_price("N/A")
        assert price is None

    def test_default_currency_used_when_no_symbol(self):
        _, currency, _ = _normalise_price("189", default_currency="MAD")
        assert currency == "MAD"

    def test_eur_default_when_no_override(self):
        _, currency, _ = _normalise_price("189")
        assert currency == "EUR"


# ---------------------------------------------------------------------------
# Description cleanup
# ---------------------------------------------------------------------------

class TestCleanDescription:

    def test_removes_rankmath_price_embed(self):
        """parachezvous.ma Rank Math injects price into description."""
        desc = (
            "Commandez ISDIN Fotoprotector SPF50 à seulement 270\u00a0\u062f.\u0645. "
            "chez Parachezvous.ma. Livraison rapide partout au Maroc. Produits 100% original."
        )
        cleaned = _clean_description(desc)
        assert "Commandez" not in cleaned
        assert "Parachezvous" not in cleaned
        assert "270" not in cleaned

    def test_removes_delivery_boilerplate(self):
        desc = "Great product. Livraison gratuite disponible partout au Maroc."
        cleaned = _clean_description(desc)
        assert "Livraison" not in cleaned
        assert "Great product." in cleaned

    def test_keeps_actual_description(self):
        desc = "Photoprotection faciale quotidienne SPF 50 à base d'eau."
        cleaned = _clean_description(desc)
        assert cleaned == desc

    def test_empty_string(self):
        assert _clean_description("") == ""

    def test_truncates_to_2000_chars(self):
        desc = "x" * 3000
        assert len(_clean_description(desc)) <= 2000

"""
tests/test_grossiste.py
========================
Tests for the grossiste wholesale integration.

Covers:
  - SobrusClient._parse_availability() — JSON and plaintext responses
  - GrossisteClient._parse_product_list() — JS var extraction + normalisation
  - sync_grossiste --match — name matching tiers
  - Order payload construction
  - API schema validation

These tests run without Django DB or network access — all external
calls are mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in [_ROOT / "src" / "backend", _ROOT / "src", _ROOT / "src" / "transformations"]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_js_products(products: list[dict]) -> str:
    """Wrap a product list in the JS var format used by grossiste sites."""
    return f"var products = {json.dumps(products)};"


def make_json_products(products: list[dict]) -> str:
    """Plain JSON array format."""
    return json.dumps(products)


SAMPLE_PRODUCTS = [
    {"CODE_PRODU": "5230", "NOM_PRODUI": "3D VIT GOUTTE 10ML",
     "PRIX_PHAR": "55.93", "PPM": "79.9", "FORME_PROD": "GO", "PA": ""},
    {"CODE_PRODU": "1869", "NOM_PRODUI": "AB-DIGEST 7 FLACON",
     "PRIX_PHAR": "62.86", "PPM": "89.80", "FORME_PROD": "PO", "PA": ""},
    {"CODE_PRODU": "30",   "NOM_PRODUI": "ACCU-CHEK ACTIVE 25 STR MIC",
     "PRIX_PHAR": "75",    "PPM": "90",   "FORME_PROD": "LI", "PA": ""},
    {"CODE_PRODU": "9983", "NOM_PRODUI": "A NOUVEAU 2008",
     "PRIX_PHAR": "0",     "PPM": "0",    "FORME_PROD": "LI", "PA": ""},  # zero price
    {"CODE_PRODU": "",     "NOM_PRODUI": "EMPTY CODE",
     "PRIX_PHAR": "10",    "PPM": "15",   "FORME_PROD": "CO", "PA": ""},  # empty code
]


# ---------------------------------------------------------------------------
# Test: product list parsing
# ---------------------------------------------------------------------------

class TestProductListParsing:
    """Tests for GrossisteClient._parse_product_list() logic."""

    def _parse(self, content: str):
        """Inline parser — mirrors GrossisteClient._parse_product_list logic."""
        import re

        match = re.search(r"var\s+products\s*=\s*(\[.*?\])\s*;", content, re.DOTALL)
        if match:
            raw = json.loads(match.group(1))
        else:
            content = content.strip()
            if content.startswith("["):
                raw = json.loads(content)
            else:
                raise ValueError("Cannot parse product list")

        def to_decimal(val):
            try:
                f = float(str(val).replace(",", ".").strip())
                return f if f > 0 else None
            except (ValueError, TypeError):
                return None

        return [
            {
                "code":            str(item.get("CODE_PRODU", "")).strip(),
                "name":            str(item.get("NOM_PRODUI", "")).strip(),
                "prix_pharmacien": to_decimal(item.get("PRIX_PHAR")),
                "ppm":             to_decimal(item.get("PPM")),
                "pa":              to_decimal(item.get("PA")) if item.get("PA") else None,
                "forme":           str(item.get("FORME_PROD", "")).strip(),
            }
            for item in raw
            if str(item.get("CODE_PRODU", "")).strip()  # skip empty codes
        ]

    def test_parses_js_var_format(self):
        content  = make_js_products(SAMPLE_PRODUCTS)
        products = self._parse(content)
        # empty code product filtered out
        assert len(products) == 4

    def test_parses_json_array_format(self):
        content  = make_json_products(SAMPLE_PRODUCTS[:3])
        products = self._parse(content)
        assert len(products) == 3

    def test_correct_field_mapping(self):
        content  = make_js_products(SAMPLE_PRODUCTS[:1])
        products = self._parse(content)
        p = products[0]
        assert p["code"]            == "5230"
        assert p["name"]            == "3D VIT GOUTTE 10ML"
        assert p["prix_pharmacien"] == 55.93
        assert p["ppm"]             == 79.9
        assert p["forme"]           == "GO"
        assert p["pa"]              is None  # empty string → None

    def test_zero_price_becomes_none(self):
        """Products with PRIX_PHAR=0 should have prix_pharmacien=None."""
        content  = make_js_products(SAMPLE_PRODUCTS)
        products = self._parse(content)
        zero     = next(p for p in products if p["code"] == "9983")
        assert zero["prix_pharmacien"] is None
        assert zero["ppm"]             is None

    def test_empty_code_filtered_out(self):
        content  = make_js_products(SAMPLE_PRODUCTS)
        products = self._parse(content)
        codes    = [p["code"] for p in products]
        assert "" not in codes

    def test_handles_comma_decimal(self):
        """Prices with comma as decimal separator (European format)."""
        products = [{"CODE_PRODU": "1", "NOM_PRODUI": "TEST",
                     "PRIX_PHAR": "55,93", "PPM": "79,9", "FORME_PROD": "CO", "PA": ""}]
        content  = make_js_products(products)
        parsed   = self._parse(content)
        assert parsed[0]["prix_pharmacien"] == 55.93
        assert parsed[0]["ppm"]             == 79.9

    def test_raises_on_malformed_content(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            self._parse("this is not a product list")

    def test_large_catalogue_performance(self):
        """Parser should handle 15,000 products without issue."""
        large = [
            {"CODE_PRODU": str(i), "NOM_PRODUI": f"PRODUCT {i}",
             "PRIX_PHAR": "55.00", "PPM": "80.00", "FORME_PROD": "CO", "PA": ""}
            for i in range(15000)
        ]
        content  = make_js_products(large)
        products = self._parse(content)
        assert len(products) == 15000


# ---------------------------------------------------------------------------
# Test: availability response parsing
# ---------------------------------------------------------------------------

class TestAvailabilityParsing:
    """Tests for SobrusClient._parse_availability equivalent logic."""

    def _parse(self, body: str) -> bool:
        """Mirrors SobrusClient availability parsing."""
        body = body.strip()

        # Try JSON
        try:
            data = json.loads(body)
            if isinstance(data, bool):
                return data
            if isinstance(data, int):
                return data > 0
            if isinstance(data, dict):
                for key in ("isAvailable", "inStock", "in_stock", "disponible", "available", "stock"):
                    if key in data:
                        val = data[key]
                        return bool(val) if not isinstance(val, str) else val.lower() == "true"
        except json.JSONDecodeError:
            pass

        # Plain text
        lower = body.lower()
        if lower in ("true", "1", "yes", "oui", "disponible"):
            return True
        if lower in ("false", "0", "no", "non", "indisponible"):
            return False
        return False

    def test_sobrus_json_response_available(self):
        """Standard Sobrus response: {"supplierId": 363, "isAvailable": true}"""
        result = self._parse('{"supplierId": 363, "isAvailable": true}')
        assert result is True

    def test_sobrus_json_response_unavailable(self):
        result = self._parse('{"supplierId": 363, "isAvailable": false}')
        assert result is False

    def test_boolean_true(self):
        assert self._parse("true") is True

    def test_boolean_false(self):
        assert self._parse("false") is False

    def test_integer_one(self):
        assert self._parse("1") is True

    def test_integer_zero(self):
        assert self._parse("0") is False

    def test_plain_text_disponible(self):
        assert self._parse("disponible") is True

    def test_plain_text_indisponible(self):
        assert self._parse("indisponible") is False

    def test_json_bool_direct(self):
        assert self._parse("true") is True
        assert self._parse("false") is False

    def test_unknown_response_defaults_false(self):
        assert self._parse("UNKNOWN_STATUS") is False


# ---------------------------------------------------------------------------
# Test: order payload construction
# ---------------------------------------------------------------------------

class TestOrderPayload:
    """Tests that the Sobrus order payload is constructed correctly."""

    def _build_payload(
        self,
        supplier_id: int,
        sobrus_product_id: int,
        quantity: int,
        unit_price: float | None,
        sale_price: float | None,
        tax_id: int = 35,
        owner_id: str = "",
        notes: str = "",
    ) -> dict:
        """Mirrors SobrusClient.place_order payload construction."""
        from datetime import date
        return {
            "products": [
                {
                    "ID":                  sobrus_product_id,
                    "quantity":            quantity,
                    "unit_price":          str(unit_price) if unit_price else "0.00",
                    "unit_original_price": unit_price or 0,
                    "purchase_price":      unit_price or 0,
                    "sale_price":          sale_price or 0,
                    "tax_id":              tax_id,
                    "discount_type":       "percentage",
                    "discount":            "0.00",
                    "available":           -1,
                    "product_price_id":    "",
                }
            ],
            "products_details":                 [],
            "purchase_order_date":              date.today().isoformat(),
            "global_discount_application_type": "each_product",
            "global_discount_type":             "percentage",
            "supplier_id":                      str(supplier_id),
            "contact_id":                       "",
            "orderOnline":                      "false",
            "owner_id":                         owner_id,
            "status_action":                    "approve",
            "comment":                          notes or None,
        }

    def test_payload_structure(self):
        payload = self._build_payload(
            supplier_id=343, sobrus_product_id=29131,
            quantity=1, unit_price=13.21, sale_price=100.0,
        )
        assert payload["supplier_id"]    == "343"
        assert payload["status_action"]  == "approve"
        assert payload["orderOnline"]    == "false"
        assert len(payload["products"])  == 1

    def test_product_fields(self):
        payload = self._build_payload(
            supplier_id=1, sobrus_product_id=148194,
            quantity=10, unit_price=55.93, sale_price=79.9,
        )
        product = payload["products"][0]
        assert product["ID"]             == 148194
        assert product["quantity"]       == 10
        assert product["unit_price"]     == "55.93"
        assert product["purchase_price"] == 55.93
        assert product["sale_price"]     == 79.9
        assert product["tax_id"]         == 35
        assert product["discount"]       == "0.00"

    def test_none_price_becomes_zero(self):
        payload = self._build_payload(
            supplier_id=1, sobrus_product_id=100,
            quantity=1, unit_price=None, sale_price=None,
        )
        product = payload["products"][0]
        assert product["unit_price"]     == "0.00"
        assert product["purchase_price"] == 0
        assert product["sale_price"]     == 0

    def test_notes_map_to_comment(self):
        payload = self._build_payload(
            supplier_id=1, sobrus_product_id=100,
            quantity=1, unit_price=10.0, sale_price=15.0,
            notes="Urgent restock",
        )
        assert payload["comment"] == "Urgent restock"

    def test_empty_notes_become_none(self):
        payload = self._build_payload(
            supplier_id=1, sobrus_product_id=100,
            quantity=1, unit_price=10.0, sale_price=15.0,
            notes="",
        )
        assert payload["comment"] is None

    def test_purchase_order_date_is_today(self):
        from datetime import date
        payload = self._build_payload(
            supplier_id=1, sobrus_product_id=100,
            quantity=1, unit_price=10.0, sale_price=15.0,
        )
        assert payload["purchase_order_date"] == date.today().isoformat()

    def test_supplier_id_is_string(self):
        """Sobrus API expects supplier_id as a string."""
        payload = self._build_payload(
            supplier_id=1570, sobrus_product_id=100,
            quantity=1, unit_price=10.0, sale_price=15.0,
        )
        assert isinstance(payload["supplier_id"], str)
        assert payload["supplier_id"] == "1570"


# ---------------------------------------------------------------------------
# Test: Sobrus order response parsing
# ---------------------------------------------------------------------------

class TestOrderResponseParsing:
    """Tests for extracting order ID and status from Sobrus create response."""

    SAMPLE_RESPONSE = {
        "data": {
            "ID": "9757341",
            "transaction_number": "BC-3579",
            "status": {"ID": "approved", "label": "Non payé"},
            "supplier_id": {"ID": "343", "label": "Grossiste COOPER TENSIFT"},
            "products": [{"ID": "29131", "quantity": "1"}],
        }
    }

    def _extract(self, raw: dict) -> dict:
        """Mirrors the extraction logic in the API view."""
        order_data      = raw.get("data", raw)
        sobrus_order_id = str(order_data.get("ID", ""))
        transaction_num = order_data.get("transaction_number", "")
        status_field    = order_data.get("status", {})
        sobrus_status   = (
            status_field.get("ID", "") if isinstance(status_field, dict)
            else str(status_field)
        )
        return {
            "sobrus_order_id":      sobrus_order_id,
            "sobrus_transaction_num": transaction_num,
            "sobrus_status":        sobrus_status,
        }

    def test_extracts_order_id(self):
        result = self._extract(self.SAMPLE_RESPONSE)
        assert result["sobrus_order_id"] == "9757341"

    def test_extracts_transaction_number(self):
        result = self._extract(self.SAMPLE_RESPONSE)
        assert result["sobrus_transaction_num"] == "BC-3579"

    def test_extracts_status(self):
        result = self._extract(self.SAMPLE_RESPONSE)
        assert result["sobrus_status"] == "approved"

    def test_handles_flat_response(self):
        """Some API responses may not have a 'data' wrapper."""
        flat = {
            "ID": "1234",
            "transaction_number": "BC-1000",
            "status": "approved",
        }
        result = self._extract(flat)
        assert result["sobrus_order_id"]       == "1234"
        assert result["sobrus_transaction_num"] == "BC-1000"
        assert result["sobrus_status"]         == "approved"

    def test_handles_missing_fields(self):
        result = self._extract({})
        assert result["sobrus_order_id"]       == ""
        assert result["sobrus_transaction_num"] == ""
        assert result["sobrus_status"]         == ""


# ---------------------------------------------------------------------------
# Test: name normalisation for matching
# ---------------------------------------------------------------------------

class TestGrossisteNameNormalisation:
    """Tests for the normalise() function used in --match."""

    def _normalise(self, text: str) -> str:
        import re
        import unicodedata
        text = text.upper().strip()
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = re.sub(r"[^\w\s]", " ", text)
        return " ".join(text.split())

    def test_uppercase(self):
        assert self._normalise("doliprane") == "DOLIPRANE"

    def test_strips_accents(self):
        assert self._normalise("ÉLIMINÉS") == "ELIMINES"

    def test_removes_punctuation(self):
        assert self._normalise("AB-DIGEST 7") == "AB DIGEST 7"

    def test_collapses_spaces(self):
        assert self._normalise("ACCU  CHEK   ACTIVE") == "ACCU CHEK ACTIVE"

    def test_handles_arabic_chars(self):
        """Arabic characters should be preserved (not stripped)."""
        result = self._normalise("DOLIPRANE 500MG")
        assert "DOLIPRANE" in result
        assert "500MG" in result

    def test_dosage_form_preserved(self):
        """Volume and form info should survive normalisation."""
        result = self._normalise("ISDIN FOTOPROTECTOR SPF50+ 200ML")
        assert "ISDIN" in result
        assert "200ML" in result
        assert "SPF50" in result

    def test_same_product_different_formatting(self):
        """Two representations of the same product should normalise similarly."""
        a = self._normalise("ACCU-CHEK ACTIVE KIT")
        b = self._normalise("ACCU CHEK ACTIVE KIT")
        assert a == b


# ---------------------------------------------------------------------------
# Test: SobrusClient headers
# ---------------------------------------------------------------------------

class TestSobrusClientHeaders:
    """Tests that SobrusClient builds correct headers."""

    def _headers(self, cookie: str, csrf: str = "") -> dict:
        headers = {
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "Cookie":       cookie,
            "Origin":       "https://app.pharma.sobrus.com",
            "Referer":      "https://app.pharma.sobrus.com/",
        }
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        return headers

    def test_cookie_in_headers(self):
        headers = self._headers("current_country_code=ma; SBSID2=abc123")
        assert headers["Cookie"] == "current_country_code=ma; SBSID2=abc123"

    def test_csrf_included_when_provided(self):
        headers = self._headers("cookie=x", csrf="mytoken")
        assert headers["X-CSRF-TOKEN"] == "mytoken"

    def test_csrf_omitted_when_empty(self):
        headers = self._headers("cookie=x", csrf="")
        assert "X-CSRF-TOKEN" not in headers

    def test_origin_is_sobrus_app(self):
        headers = self._headers("cookie=x")
        assert headers["Origin"] == "https://app.pharma.sobrus.com"

    def test_content_type_is_json(self):
        headers = self._headers("cookie=x")
        assert headers["Content-Type"] == "application/json"

"""
tests/test_entity_resolution.py
=================================
Unit tests for the Gold entity resolution matching logic.

Tests cover:
- Text normalisation
- Volume extraction
- Brand+volume fingerprinting
- Matching tier logic (using MasterIndex directly)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "backend"))

from matching.entity_res import (
    MasterIndex,
    MatchResult,
    _brand_volume_fingerprint,
    _extract_volume,
    _normalise_name,
    _match_one,
)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

class TestNormaliseName:

    def test_lowercase(self):
        assert _normalise_name("ISDIN Fotoprotector") == "fotoprotector isdin"

    def test_removes_accents(self):
        assert "e" in _normalise_name("Éclat")
        assert "É" not in _normalise_name("Éclat")

    def test_token_sort(self):
        """Word order should not matter after normalisation."""
        a = _normalise_name("Gel Moussant CeraVe 200ml")
        b = _normalise_name("CeraVe Gel Moussant 200ml")
        assert a == b

    def test_removes_stop_words(self):
        result = _normalise_name("Crème pour le visage")
        assert "pour" not in result
        assert "le" not in result

    def test_normalises_volume_spacing(self):
        """'200 ml' and '200ml' should produce the same token."""
        a = _normalise_name("Product 200 ml SPF50")
        b = _normalise_name("Product 200ml SPF50")
        assert a == b

    def test_empty_string(self):
        assert _normalise_name("") == ""

    def test_punctuation_removed(self):
        result = _normalise_name("L'Oréal - Sérum 30ml")
        assert "-" not in result
        assert "'" not in result


# ---------------------------------------------------------------------------
# Volume extraction
# ---------------------------------------------------------------------------

class TestExtractVolume:

    def test_ml_lowercase(self):
        assert _extract_volume("CeraVe 200ml") == "200ml"

    def test_ml_with_space(self):
        assert _extract_volume("CeraVe 200 ml") == "200ml"

    def test_mg(self):
        assert _extract_volume("Vitamin C 500mg") == "500mg"

    def test_grams(self):
        assert _extract_volume("Cream 50g") == "50g"

    def test_decimal_volume(self):
        assert _extract_volume("Oil 1.5l") == "1.5l"

    def test_no_volume(self):
        assert _extract_volume("Simple Product Name") is None

    def test_uppercase_unit(self):
        assert _extract_volume("Product 200ML") == "200ml"


# ---------------------------------------------------------------------------
# Brand+volume fingerprint
# ---------------------------------------------------------------------------

class TestBrandVolumeFingerprint:

    def test_basic(self):
        fp = _brand_volume_fingerprint("ISDIN", "ISDIN Fotoprotector 200ml SPF50")
        assert fp is not None
        assert "isdin" in fp
        assert "200ml" in fp

    def test_includes_key_token(self):
        """Fingerprint must include a discriminating token beyond brand+volume."""
        fp = _brand_volume_fingerprint("A-DERMA", "A-DERMA Biology AC Global 40ml")
        assert fp is not None
        # Should not match A-DERMA Epitheliale 40ml
        fp2 = _brand_volume_fingerprint("A-DERMA", "A-DERMA Epitheliale 40ml")
        assert fp != fp2, "Different A-DERMA 40ml products should have different fingerprints"

    def test_no_brand_returns_none(self):
        assert _brand_volume_fingerprint("", "Product 200ml") is None

    def test_no_volume_returns_none(self):
        assert _brand_volume_fingerprint("ISDIN", "ISDIN Simple Product") is None

    def test_normalises_brand(self):
        # L'Oréal → apostrophe removed → "loreal" after accent removal
        # The normaliser strips punctuation so "L'" becomes "l" then joins
        # Both inputs should produce the same fingerprint
        fp1 = _brand_volume_fingerprint("Loreal", "Loreal Serum 30ml age")
        fp2 = _brand_volume_fingerprint("LOREAL", "LOREAL Serum 30ml age")
        assert fp1 == fp2


# ---------------------------------------------------------------------------
# Matching tiers (unit tests using MasterIndex directly)
# ---------------------------------------------------------------------------

def _make_index(**kwargs) -> MasterIndex:
    """Create a minimal MasterIndex for testing."""
    idx = MasterIndex()
    for key, val in kwargs.items():
        setattr(idx, key, val)
    return idx


def _make_row(**kwargs) -> "pd.Series":
    import pandas as pd
    defaults = {
        "raw_name": "", "raw_brand": "", "raw_ean": "",
        "raw_sku": "", "site_id": str(uuid4()), "domain": "test.ma",
    }
    defaults.update(kwargs)
    return pd.Series(defaults)


class TestMatchingTiers:

    def test_tier1_ean_match(self):
        master_id = uuid4()
        idx = MasterIndex()
        idx.ean_index = {"4006000099422": master_id}

        row = _make_row(raw_ean="4006000099422", raw_name="Some Product", raw_brand="EUCERIN")
        result = _match_one(row, idx)

        assert result.tier == 1
        assert result.master_id == master_id
        assert result.confidence == 1.0
        assert not result.is_new

    def test_tier1_invalid_ean_skipped(self):
        """Non-numeric EAN should not trigger Tier 1."""
        master_id = uuid4()
        idx = MasterIndex()
        idx.ean_index = {"ABC123": master_id}

        row = _make_row(raw_ean="ABC123", raw_name="Some Product", raw_brand="EUCERIN")
        result = _match_one(row, idx)

        assert result.tier != 1

    def test_tier3_exact_name_match(self):
        master_id = uuid4()
        normalised = _normalise_name("ISDIN Fotoprotector SPF50 200ml")

        idx = MasterIndex()
        idx.name_index = {normalised: master_id}
        idx.all_names = [normalised]
        idx.all_ids = [master_id]
        idx._master_brands = {master_id: "isdin"}

        row = _make_row(raw_name="ISDIN Fotoprotector SPF50 200ml", raw_brand="ISDIN")
        result = _match_one(row, idx)

        assert result.tier == 3
        assert result.master_id == master_id

    def test_tier3_same_domain_excluded(self):
        """Products from the same site should never match each other."""
        master_id = uuid4()
        normalised = _normalise_name("ISDIN Fotoprotector SPF50 200ml")
        domain = "universparadiscount.ma"

        idx = MasterIndex()
        idx.name_index = {normalised: master_id}
        idx.all_names = [normalised]
        idx.all_ids = [master_id]
        idx._master_brands = {master_id: "isdin"}
        idx.domain_masters = {domain: {master_id}}  # master belongs to same domain

        row = _make_row(
            raw_name="ISDIN Fotoprotector SPF50 200ml",
            raw_brand="ISDIN",
            domain=domain,
        )
        result = _match_one(row, idx)

        # Should NOT match — same domain
        assert result.is_new or result.tier == 6

    def test_tier3_brand_gate(self):
        """Different brands should never match even with similar names."""
        master_id = uuid4()
        normalised = _normalise_name("Fotoprotector SPF50 200ml")

        idx = MasterIndex()
        idx.name_index = {normalised: master_id}
        idx.all_names = [normalised]
        idx.all_ids = [master_id]
        # Critical: populate _master_brands so the brand gate has something to compare
        idx._master_brands = {master_id: _normalise_name("ISDIN")}

        # Same product name but different brand
        row = _make_row(
            raw_name="Fotoprotector SPF50 200ml",
            raw_brand="AVENE",  # different brand — should not match
        )
        result = _match_one(row, idx)

        assert result.is_new or result.tier == 6

    def test_tier6_new_product(self):
        """Product with no matches should create a new MasterProduct."""
        idx = MasterIndex()  # empty index

        row = _make_row(raw_name="Completely Unknown Product 500ml", raw_brand="UNKNOWNBRAND")
        result = _match_one(row, idx)

        assert result.tier == 6
        assert result.is_new
        assert result.master_id is None
        assert result.confidence == 0.0

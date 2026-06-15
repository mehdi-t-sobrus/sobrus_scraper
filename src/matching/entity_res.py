"""
src/matching/entity_res.py
===========================
Gold-layer entity resolution engine.

Reads Silver Parquet files, matches each SiteProduct to a canonical
MasterProduct using a 6-tier matching strategy, and writes results to
the Django PostgreSQL/TimescaleDB Gold warehouse.

Matching tiers (in order of priority)
---------------------------------------
Tier 1 — EAN/GTIN exact match
    If two products share the same barcode they are unambiguously the same.
    Source: raw_ean from JSON-LD gtin13/gtin8 or OG product:retailer_item_id.

Tier 2 — SKU + domain exact match
    Same product re-scraped from the same site. Matches on (site_id, raw_sku).

Tier 3 — Normalised name token-sort ≥ 0.95
    Lower-case, de-accent, remove stop words, sort tokens alphabetically,
    then RapidFuzz token_sort_ratio. Catches word-order variations.

Tier 3.5 — Brand + volume fingerprint
    Normalised brand name + extracted volume/dosage.
    "ISDIN_200ml" matches across sites even with different name suffixes.

Tier 4 — Vector cosine similarity ≥ 0.90
    Sentence embedding of the normalised product name, cosine search via
    pgvector HNSW index. Catches semantic matches missed by string methods.

Tier 5 — Vector cosine 0.65–0.89
    Flag for human review (status = UNDER_REVIEW, match_confidence set).

Tier 6 — No match / below 0.65
    Create a new MasterProduct.

Architecture contract (CLAUDE.md §1)
--------------------------------------
- Reads Silver Parquet from data/silver/ (dev) or R2 (prod).
- Writes to Django ORM via async bulk operations.
- Never reads Bronze directly.
- Never writes to Silver.

Running
-------
    python manage.py run_matching                         # all sites
    python manage.py run_matching --site universparadiscount.ma
    python manage.py run_matching --date 2026-06-10
    python manage.py run_matching --dry-run               # no DB writes
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from rapidfuzz import fuzz, process as rf_process

# Load matching-specific env first (R2_LOCAL_DEV_MODE, thresholds etc.)
load_dotenv(Path(__file__).resolve().parent / ".env")
# Load backend env for Django settings and DB credentials
load_dotenv(Path(__file__).resolve().parent.parent / "backend" / ".env")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django
django.setup()

from django.db import transaction

from products.models import DailyPriceLog, MasterProduct, SiteProduct
from scraper_admin.models import SiteConfig

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIER3_THRESHOLD   = 0.95   # Auto-match: normalised name similarity
TIER4_AUTO        = 0.90   # Auto-match: vector cosine similarity
TIER5_REVIEW      = 0.65   # Flag for human review
BATCH_SIZE        = 500    # DB write batch size
EMBEDDING_DIM     = 768    # paraphrase-multilingual-mpnet-base-v2 output dim

# French + English stop words relevant to pharma product names
_STOP_WORDS: frozenset[str] = frozenset({
    "le", "la", "les", "de", "du", "des", "un", "une",
    "pour", "avec", "sans", "et", "en", "au", "aux",
    "the", "for", "with", "without", "and", "to", "of",
    "ml", "mg", "g", "kg", "l", "cl",  # units handled separately
    "x",  # as in "2 x 50ml"
})

# Volume/dosage pattern: captures "200ml", "50 ml", "1.5l", "500mg" etc.
_VOLUME_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|cl|l|mg|g|kg|ui|iu|mcg|µg)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalise_name(name: str) -> str:
    """
    Normalise a product name for fuzzy string matching.

    Steps:
    1. Lower-case
    2. Remove accents (NFD decomposition + strip combining chars)
    3. Remove punctuation except digits, letters, spaces
    4. Tokenise, remove stop words
    5. Sort tokens alphabetically (token_sort makes word order irrelevant)

    Example:
        "Gel Moussant CeraVe 200 ml" → "cerave gel moussant 200"
        "CeraVe Gel Moussant 200ml"  → "cerave gel moussant 200"
    """
    if not name:
        return ""

    # Lowercase
    s = name.lower()

    # Remove accents
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")

    # Normalise volume units: "200ml" → "200ml", "200 ml" → "200ml"
    s = re.sub(r"(\d+)\s+(ml|cl|l|mg|g|kg|ui|iu|mcg)", r"\1\2", s)

    # Remove non-alphanumeric except spaces
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Tokenise, remove stop words and single characters
    tokens = [
        t for t in s.split()
        if t not in _STOP_WORDS and len(t) > 1
    ]

    # Sort tokens for order-invariant comparison
    tokens.sort()

    return " ".join(tokens)


def _extract_volume(name: str) -> str | None:
    """
    Extract normalised volume/dosage from a product name.
    Returns "200ml", "500mg" etc. or None.
    """
    match = _VOLUME_RE.search(name)
    if not match:
        return None
    amount = match.group(1).replace(",", ".")
    unit = match.group(2).lower()
    return f"{amount}{unit}"


def _brand_volume_fingerprint(brand: str, name: str) -> str | None:
    """
    Create a brand+volume+key_token fingerprint for Tier 3.5 matching.

    Format: "<brand>_<volume>_<key_token>"
    Example: "isdin_200ml_fotoprotector"

    The key token is the first non-brand, non-stop-word, non-unit token
    from the normalised name — this prevents "aderma_40ml" from matching
    ALL A-DERMA 40ml products regardless of product type.

    Returns None if brand, volume, or a discriminating token is missing.
    """
    if not brand:
        return None
    normalised_brand = _normalise_name(brand).replace(" ", "")
    if not normalised_brand:
        return None

    volume = _extract_volume(name)
    if not volume:
        return None

    # Extract key token: first token not in the brand and not a unit
    brand_tokens = set(normalised_brand.split())
    name_normalised = _normalise_name(name)
    key_token = None
    for token in name_normalised.split():
        if token not in brand_tokens and not re.match(r"^\d+[a-z]+$", token):
            key_token = token
            break

    if not key_token:
        return None

    return f"{normalised_brand}_{volume}_{key_token}"


# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded — only imported when needed)
# ---------------------------------------------------------------------------

_embedding_model = None


def _get_embedding_model():
    """
    Lazy-load the sentence transformer model.
    Downloads ~420MB on first use, cached to ~/.cache/huggingface/
    """
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model paraphrase-multilingual-mpnet-base-v2...")
            _embedding_model = SentenceTransformer(
                "paraphrase-multilingual-mpnet-base-v2"
            )
            logger.info("Embedding model loaded.")
        except ImportError:
            logger.warning(
                "sentence-transformers not installed. Tier 4 vector matching disabled. "
                "Install with: pip install sentence-transformers"
            )
            return None
    return _embedding_model


def _embed(texts: list[str]) -> np.ndarray | None:
    """
    Compute embeddings for a list of texts.
    Returns (N, 768) float32 array or None if model unavailable.
    """
    model = _get_embedding_model()
    if model is None:
        return None
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


# ---------------------------------------------------------------------------
# Silver Parquet reader
# ---------------------------------------------------------------------------

def _load_silver_records(
    silver_root: Path,
    site_domain: str | None = None,
    target_date: str | None = None,
) -> pd.DataFrame:
    """
    Load Silver Parquet records, optionally filtered by domain and date.

    DuckDB writes partitioned Parquet with PARTITION_BY (domain, fetched_date),
    which encodes those values in the folder path and strips them from the file.
    We reconstruct them from the path when reading.
    """
    files = list(silver_root.rglob("*.parquet"))

    # Filter by domain partition folder
    if site_domain:
        files = [f for f in files if f"domain={site_domain}" in str(f)]

    # Filter by date partition folder
    if target_date:
        files = [f for f in files if f"fetched_date={target_date}" in str(f)]

    if not files:
        logger.warning(
            "No Silver Parquet files found (domain=%s, date=%s) in %s.",
            site_domain, target_date, silver_root,
        )
        return pd.DataFrame()

    dfs = []
    for f in sorted(files):
        try:
            df = pd.read_parquet(f)

            # Reconstruct partition columns from folder path
            # Path looks like: .../domain=cotepara.ma/fetched_date=2026-06-10/xxx.parquet
            parts = f.parts
            for part in parts:
                if part.startswith("domain=") and "domain" not in df.columns:
                    df["domain"] = part.split("=", 1)[1]
                if part.startswith("fetched_date=") and "fetched_date" not in df.columns:
                    df["fetched_date"] = part.split("=", 1)[1]

            dfs.append(df)
        except Exception as exc:
            logger.error("Failed to read %s: %s", f, exc)

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    logger.info(
        "Loaded %d Silver records from %d files.",
        len(df), len(files),
    )
    return df


# ---------------------------------------------------------------------------
# Master product catalogue (in-memory index for matching)
# ---------------------------------------------------------------------------

@dataclass
class MasterIndex:
    """
    In-memory index of existing MasterProduct records for fast lookups.
    Rebuilt at the start of each matching run.
    """
    # EAN → MasterProduct.id
    ean_index:         dict[str, UUID] = field(default_factory=dict)
    # (site_id, sku) → MasterProduct.id
    sku_index:         dict[tuple, UUID] = field(default_factory=dict)
    # normalised_name → MasterProduct.id
    name_index:        dict[str, UUID] = field(default_factory=dict)
    # brand_volume_fingerprint → MasterProduct.id
    fingerprint_index: dict[str, UUID] = field(default_factory=dict)
    # id → MasterProduct instance (for updates)
    by_id:             dict[UUID, MasterProduct] = field(default_factory=dict)
    # All normalised names (for RapidFuzz bulk search)
    all_names:         list[str] = field(default_factory=list)
    all_ids:           list[UUID] = field(default_factory=list)
    # domain → set of MasterProduct.ids already linked to that domain
    # Used to prevent matching a product against another from the same site
    domain_masters:    dict[str, set] = field(default_factory=dict)
    # master_id → normalised brand name (for brand gate in Tier 3/4/5)
    _master_brands:    dict = field(default_factory=dict)
    # Embeddings matrix (N, 768) — populated if model is available
    embeddings:        np.ndarray | None = None


def _build_master_index() -> MasterIndex:
    """
    Load all MasterProduct records into memory and build lookup indexes.
    Called once at the start of a matching run.
    """
    idx = MasterIndex()

    masters = list(MasterProduct.objects.filter(
        status__in=[MasterProduct.Status.ACTIVE, MasterProduct.Status.UNDER_REVIEW]
    ).values(
        "id", "name", "brand", "ean"
    ))

    # Load embeddings via raw SQL — name_embedding is a pgvector column added
    # via migration 0003 raw SQL, not a Django model field, so ORM .values()
    # doesn't know about it. Raw query returns the vector as a Python list.
    embedding_map: dict = {}
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name_embedding::text
                FROM products_masterproduct
                WHERE status IN ('active', 'under_review')
                  AND name_embedding IS NOT NULL
                """
            )
            for master_id, vec_text in cursor.fetchall():
                if vec_text:
                    # pgvector returns "[0.1,0.2,...]" — parse to float list
                    vec = [float(x) for x in vec_text.strip("[]").split(",")]
                    embedding_map[master_id] = vec
        logger.info("Loaded %d stored embeddings from pgvector.", len(embedding_map))
    except Exception as exc:
        logger.warning("Could not load embeddings from pgvector: %s", exc)

    logger.info("Building master index from %d existing MasterProducts.", len(masters))

    for row in masters:
        mid = row["id"]

        # EAN index
        if row["ean"]:
            idx.ean_index[row["ean"].strip()] = mid

        # Name index
        normalised = _normalise_name(row["name"])
        if normalised:
            idx.name_index[normalised] = mid
            idx.all_names.append(normalised)
            idx.all_ids.append(mid)

        # Brand index — normalised for brand gate in Tier 3/4/5
        if row["brand"]:
            idx._master_brands[mid] = _normalise_name(row["brand"])

        # Brand+volume fingerprint index
        fp = _brand_volume_fingerprint(row["brand"], row["name"])
        if fp:
            idx.fingerprint_index[fp] = mid

    # Build domain → master_ids map from existing SiteProducts.
    # Used to prevent same-site matching — a product from site A should never
    # be merged with another product already linked to site A.
    for sp in SiteProduct.objects.select_related("site").values(
        "master_product_id", "site__domain"
    ):
        domain = sp["site__domain"]
        mid = sp["master_product_id"]
        if domain not in idx.domain_masters:
            idx.domain_masters[domain] = set()
        idx.domain_masters[domain].add(mid)

    # Build embedding matrix from stored pgvector values
    valid_vecs: list[tuple[UUID, np.ndarray]] = []
    str_embedding_map = {str(k): v for k, v in embedding_map.items()}

    for row in masters:
        vec_data = str_embedding_map.get(str(row["id"]))
        if vec_data is not None:
            valid_vecs.append((row["id"], np.array(vec_data, dtype=np.float32)))

    if valid_vecs:
        ids, vecs = zip(*valid_vecs)
        idx.embeddings = np.stack(vecs)
        idx._embedding_ids = list(ids)
    else:
        idx.embeddings = None
        idx._embedding_ids = []

    logger.info(
        "Master index built: %d EANs, %d names, %d fingerprints, %d embeddings.",
        len(idx.ean_index), len(idx.name_index),
        len(idx.fingerprint_index),
        len(getattr(idx, "_embedding_ids", [])),
    )
    return idx


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """Result of matching one SiteProduct row to a MasterProduct."""
    master_id:    UUID | None
    tier:         int
    confidence:   float
    is_new:       bool = False
    needs_review: bool = False


def _match_one(row: pd.Series, idx: MasterIndex) -> MatchResult:
    """
    Run the 6-tier matching pipeline for a single Silver record.

    Parameters
    ----------
    row:
        A single row from the Silver DataFrame.
    idx:
        The pre-built MasterIndex.

    Returns
    -------
    MatchResult
        The best match found and its tier/confidence.
    """
    raw_ean   = str(row.get("raw_ean", "") or "").strip()
    raw_sku   = str(row.get("raw_sku", "") or "").strip()
    raw_name  = str(row.get("raw_name", "") or "").strip()
    raw_brand = str(row.get("raw_brand", "") or "").strip()
    site_id   = str(row.get("site_id", "") or "").strip()
    domain    = str(row.get("domain", "") or "").strip()

    # Set of MasterProduct IDs already linked to this domain.
    # These are excluded from Tier 3+ matching — same site never merges.
    # EAN (Tier 1) and SKU (Tier 2) are allowed cross-domain AND same-domain
    # because barcodes are globally unique identifiers, not site-local.
    same_domain_masters: set = idx.domain_masters.get(domain, set())

    # ------------------------------------------------------------------
    # Tier 1 — EAN exact match
    # ------------------------------------------------------------------
    if raw_ean and re.match(r"^\d{8,14}$", raw_ean):
        if raw_ean in idx.ean_index:
            return MatchResult(
                master_id=idx.ean_index[raw_ean],
                tier=1, confidence=1.0,
            )

    # ------------------------------------------------------------------
    # Tier 2 — SKU + site exact match
    # ------------------------------------------------------------------
    if raw_sku and site_id:
        key = (site_id, raw_sku)
        if key in idx.sku_index:
            return MatchResult(
                master_id=idx.sku_index[key],
                tier=2, confidence=1.0,
            )

    # ------------------------------------------------------------------
    # Tier 3 — Normalised name token-sort similarity ≥ 0.95
    # Same-domain excluded — a site's own products never merge.
    # Brand gate — different brands never match regardless of name similarity.
    # ------------------------------------------------------------------
    normalised = _normalise_name(raw_name)
    normalised_brand = _normalise_name(raw_brand)

    # Exact normalised name match first (fastest)
    if normalised and normalised in idx.name_index:
        candidate = idx.name_index[normalised]
        if candidate not in same_domain_masters:
            # Brand gate — skip if brands are known and don't match
            candidate_brand = idx._master_brands.get(candidate, "")
            if not normalised_brand or not candidate_brand or normalised_brand == candidate_brand:
                return MatchResult(
                    master_id=candidate,
                    tier=3, confidence=1.0,
                )

    # RapidFuzz best match against all normalised names
    if normalised and idx.all_names:
        # Filter to candidate indices not from the same domain
        # AND with matching brand (if brand is known)
        eligible_names = []
        for name, mid in zip(idx.all_names, idx.all_ids):
            if mid in same_domain_masters:
                continue
            # Brand gate — if we have a brand, only consider same-brand masters
            if normalised_brand and mid in idx._master_brands:
                candidate_brand = idx._master_brands.get(mid, "")
                if candidate_brand and candidate_brand != normalised_brand:
                    continue
            eligible_names.append((name, mid))

        if eligible_names:
            names_only = [n for n, _ in eligible_names]
            best = rf_process.extractOne(
                normalised,
                names_only,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=TIER3_THRESHOLD * 100,
            )
            if best:
                best_name, score, best_idx = best
                return MatchResult(
                    master_id=eligible_names[best_idx][1],
                    tier=3, confidence=round(score / 100, 4),
                )

    # ------------------------------------------------------------------
    # Tier 3.5 — Brand + volume fingerprint
    # Same-domain excluded
    # ------------------------------------------------------------------
    fp = _brand_volume_fingerprint(raw_brand, raw_name)
    if fp and fp in idx.fingerprint_index:
        candidate = idx.fingerprint_index[fp]
        if candidate not in same_domain_masters:
            return MatchResult(
                master_id=candidate,
                tier=4, confidence=0.92,
            )

    # ------------------------------------------------------------------
    # Tier 4 — Vector cosine similarity
    # Same-domain excluded
    # ------------------------------------------------------------------
    if idx.embeddings is not None and normalised:
        query_vec = _embed([normalised])
        if query_vec is not None:
            sims = idx.embeddings @ query_vec[0]
            # Sort by similarity descending, skip same-domain masters
            sorted_indices = np.argsort(sims)[::-1]
            for best_sim_idx in sorted_indices:
                candidate_id = idx._embedding_ids[best_sim_idx]
                if candidate_id in same_domain_masters:
                    continue
                # Brand gate
                if normalised_brand and candidate_id in idx._master_brands:
                    candidate_brand = idx._master_brands.get(candidate_id, "")
                    if candidate_brand and candidate_brand != normalised_brand:
                        continue
                best_sim = float(sims[best_sim_idx])
                if best_sim >= TIER4_AUTO:
                    return MatchResult(
                        master_id=candidate_id,
                        tier=4, confidence=round(best_sim, 4),
                    )
                elif best_sim >= TIER5_REVIEW:
                    return MatchResult(
                        master_id=candidate_id,
                        tier=5, confidence=round(best_sim, 4),
                        needs_review=True,
                    )
                break  # below TIER5_REVIEW threshold — no point checking further

    # ------------------------------------------------------------------
    # Tier 6 — No match → create new MasterProduct
    # ------------------------------------------------------------------
    return MatchResult(
        master_id=None,
        tier=6, confidence=0.0,
        is_new=True,
    )


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def _pick_best_image(existing: list, incoming: list, incoming_domain: str) -> list:
    """
    Image selection strategy — pick the highest-quality source.

    Priority order (highest to lowest):
    1. Parachezvous.ma — multiple high-res images (1000x1000), clean CDN URLs
    2. Beautymall.ma   — multiple images, WooCommerce CDN
    3. Cotepara.ma     — single image but reliable
    4. Beautymarket.ma — Shopify CDN, good quality
    5. Universparadiscount.ma — PrestaShop CDN, acceptable

    If the master already has images from a higher-priority source, keep them.
    If incoming is from a higher-priority source, replace.
    """
    DOMAIN_PRIORITY = {
        "parachezvous.ma":       1,
        "beautymall.ma":         2,
        "cotepara.ma":           3,
        "beautymarket.ma":       4,
        "universparadiscount.ma": 5,
    }

    if not incoming:
        return existing
    if not existing:
        return incoming

    # Determine source domain of existing images from URL
    existing_domain = None
    if existing:
        first_url = existing[0] if existing else ""
        for domain in DOMAIN_PRIORITY:
            if domain.replace(".ma", "") in first_url.lower():
                existing_domain = domain
                break

    existing_priority = DOMAIN_PRIORITY.get(existing_domain, 99)
    incoming_priority = DOMAIN_PRIORITY.get(incoming_domain, 99)

    # Lower number = higher priority
    if incoming_priority < existing_priority:
        return incoming

    # Same priority — take the one with more images
    if incoming_priority == existing_priority and len(incoming) > len(existing):
        return incoming

    return existing


def _upsert_master_product(
    row: pd.Series,
    match: MatchResult,
    now: datetime,
) -> MasterProduct:
    """
    Create or update a MasterProduct based on the match result.
    Enriches the master with images, description, tags and other
    metadata using a priority-based strategy — best source wins.
    Called inside a transaction — do not commit here.
    """
    domain = str(row.get("domain", "") or "")

    # Parse incoming enrichment fields
    raw_images = []
    try:
        import json as _json
        raw_images = _json.loads(row.get("raw_images") or "[]") or []
        if isinstance(raw_images, str):
            raw_images = [raw_images]
    except Exception:
        pass

    raw_description = str(row.get("raw_description", "") or "").strip()
    raw_tags = []
    try:
        raw_tags = _json.loads(row.get("raw_tags") or "[]") or []
    except Exception:
        pass

    if match.is_new:
        master = MasterProduct(
            name=        str(row.get("raw_name", "") or "")[:512],
            brand=       str(row.get("raw_brand", "") or "")[:128],
            ean=         str(row.get("raw_ean", "") or "")[:14],
            mpn=         str(row.get("raw_mpn", "") or "")[:128],
            category=    str(row.get("raw_category", "") or "")[:255],
            description= raw_description,
            image_urls=  raw_images,
            tags=        raw_tags,
            status=      MasterProduct.Status.ACTIVE,
            match_confidence=1.0,
            last_matched_at=now,
        )
        master.save()
        return master

    master = MasterProduct.objects.get(id=match.master_id)

    update_fields = ["status", "match_confidence", "last_matched_at"]

    if match.needs_review:
        master.status = MasterProduct.Status.UNDER_REVIEW
    master.match_confidence = match.confidence
    master.last_matched_at = now

    # Fill missing identifier fields
    if not master.ean and row.get("raw_ean"):
        master.ean = str(row["raw_ean"])[:14]
        update_fields.append("ean")
    if not master.mpn and row.get("raw_mpn"):
        master.mpn = str(row["raw_mpn"])[:128]
        update_fields.append("mpn")
    if not master.brand and row.get("raw_brand"):
        master.brand = str(row["raw_brand"])[:128]
        update_fields.append("brand")
    if not master.category and row.get("raw_category"):
        master.category = str(row["raw_category"])[:255]
        update_fields.append("category")

    # Fill description if missing
    if not master.description and raw_description:
        master.description = raw_description
        update_fields.append("description")

    # Enrich tags — merge without duplicates
    if raw_tags:
        existing_tags = set(master.tags or [])
        merged_tags = list(existing_tags | set(raw_tags))
        if merged_tags != master.tags:
            master.tags = merged_tags
            update_fields.append("tags")

    # Image selection — use priority-based strategy
    best_images = _pick_best_image(master.image_urls or [], raw_images, domain)
    if best_images != master.image_urls:
        master.image_urls = best_images
        update_fields.append("image_urls")

    master.save(update_fields=update_fields)
    return master


def _upsert_site_product(
    row: pd.Series,
    master: MasterProduct,
    site: SiteConfig,
    match: MatchResult,
    now: datetime,
) -> SiteProduct | None:
    """
    Create or update a SiteProduct linking a site listing to a MasterProduct.

    Strategy: check-then-act to avoid constraint violations entirely.

    1. If a SiteProduct exists for this product_url → update it in place.
    2. Else if a SiteProduct exists for this (master, site) pair → update only
       if the new match score is better, otherwise keep the existing one.
    3. Else → create a new SiteProduct.

    This approach never triggers constraint violations so the transaction
    stays clean and the terminal stays quiet.
    """
    url = str(row.get("url", "") or "")
    # Extract primary image URL from raw_images JSON
    image_url = ""
    try:
        import json as _json
        images = _json.loads(row.get("raw_images") or "[]") or []
        image_url = images[0] if images else ""
    except Exception:
        pass

    price = row.get("raw_price") if pd.notna(row.get("raw_price")) else None

    # Case 1 — same URL already exists (re-scrape of the same page)
    existing_by_url = SiteProduct.objects.filter(product_url=url).first()
    if existing_by_url:
        existing_by_url.master_product = master
        existing_by_url.current_price  = price
        existing_by_url.in_stock       = bool(row.get("in_stock", True))
        existing_by_url.match_score    = match.confidence
        existing_by_url.last_scraped_at = now
        if image_url:
            existing_by_url.image_url = image_url
        existing_by_url.save(update_fields=[
            "master_product", "current_price", "in_stock",
            "match_score", "last_scraped_at", "image_url",
        ])
        return existing_by_url

    # Case 2 — different URL but (master, site) pair already exists
    existing_by_pair = SiteProduct.objects.filter(
        master_product=master, site=site
    ).first()
    if existing_by_pair:
        if match.confidence > existing_by_pair.match_score:
            existing_by_pair.current_price  = price
            existing_by_pair.in_stock       = bool(row.get("in_stock", True))
            existing_by_pair.match_score    = match.confidence
            existing_by_pair.last_scraped_at = now
            existing_by_pair.save(update_fields=[
                "current_price", "in_stock", "match_score", "last_scraped_at",
            ])
        return existing_by_pair

    # Case 3 — genuinely new (master, site, url) combination
    return SiteProduct.objects.create(
        product_url=url,
        master_product=master,
        site=site,
        raw_name=        str(row.get("raw_name", "") or "")[:512],
        raw_brand=       str(row.get("raw_brand", "") or "")[:128],
        raw_ean=         str(row.get("raw_ean", "") or "")[:14],
        raw_category=    str(row.get("raw_category", "") or "")[:255],
        raw_description= str(row.get("raw_description", "") or ""),
        image_url=       image_url,
        current_price=   price,
        currency=        str(row.get("raw_currency", "MAD") or "MAD")[:3],
        in_stock=        bool(row.get("in_stock", True)),
        match_score=     match.confidence,
        last_scraped_at= now,
    )


def _insert_price_log(
    sp: SiteProduct | None,
    master: MasterProduct,
    site: SiteConfig,
    row: pd.Series,
    now: datetime,
) -> None:
    """Insert a DailyPriceLog row for this scrape observation."""
    if sp is None:
        return
    price = row.get("raw_price")
    if price is None or pd.isna(price):
        return

    DailyPriceLog.objects.create(
        site_product=sp,
        master_product=master,
        site=site,
        price=float(price),
        currency=str(row.get("raw_currency", "MAD") or "MAD")[:3],
        in_stock=bool(row.get("in_stock", True)),
        logged_at=now,
    )


# ---------------------------------------------------------------------------
# Embedding storage (updates MasterProduct.name_embedding via raw SQL)
# ---------------------------------------------------------------------------

def _store_embeddings(masters_to_embed: list[tuple[UUID, str]]) -> None:
    """
    Compute and store embeddings for MasterProducts that don't have one yet.

    Parameters
    ----------
    masters_to_embed:
        List of (master_id, normalised_name) tuples.
    """
    if not masters_to_embed:
        return

    model = _get_embedding_model()
    if model is None:
        return

    from django.db import connection

    ids, names = zip(*masters_to_embed)
    logger.info("Computing embeddings for %d new MasterProducts...", len(names))
    vecs = model.encode(list(names), normalize_embeddings=True, show_progress_bar=True)

    with connection.cursor() as cursor:
        for master_id, vec in zip(ids, vecs):
            vec_str = "[" + ",".join(f"{v:.6f}" for v in vec.tolist()) + "]"
            cursor.execute(
                "UPDATE products_masterproduct SET name_embedding = %s WHERE id = %s",
                [vec_str, str(master_id)],
            )
    logger.info("Embeddings stored for %d MasterProducts.", len(ids))


# ---------------------------------------------------------------------------
# R2 Silver reader
# ---------------------------------------------------------------------------

def _download_silver_from_r2(
    site_domain: str | None,
    target_date: str | None,
    local_cache_dir: Path,
) -> Path:
    """
    Download Silver Parquet files from Cloudflare R2 to a local cache directory.

    R2 path structure mirrors the local dev structure:
        s3://<bucket>/silver/products/domain=<domain>/fetched_date=<date>/<uuid>.parquet

    Files are cached locally and only re-downloaded if the R2 object is newer
    than the local copy (via ETag comparison).

    Parameters
    ----------
    site_domain:
        If provided, only download files for this domain.
    target_date:
        If provided, only download files for this date (YYYY-MM-DD).
    local_cache_dir:
        Local directory to cache downloaded Parquet files.

    Returns
    -------
    Path
        Path to the local cache directory (mirrors R2 structure).
    """
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    bucket    = os.getenv("R2_SILVER_BUCKET", "pipeline-silver")
    endpoint  = os.getenv("R2_ENDPOINT_URL", "")
    access_key = os.getenv("R2_ACCESS_KEY_ID", "")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "")

    if not all([endpoint, access_key, secret_key]):
        raise EnvironmentError(
            "R2 credentials not configured. Set R2_ENDPOINT_URL, "
            "R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY in matching/.env — "
            "or set R2_LOCAL_DEV_MODE=True to use local data/silver/ instead."
        )

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to create R2 client: {exc}") from exc

    # Build S3 prefix to list
    prefix = "silver/products/"
    if site_domain:
        prefix += f"domain={site_domain}/"
    if target_date:
        prefix += f"fetched_date={target_date}/"

    logger.info("Listing R2 Silver objects: s3://%s/%s", bucket, prefix)

    downloaded = 0
    skipped = 0

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".parquet"):
                    continue

                # Mirror R2 path to local cache
                # key = "silver/products/domain=x.ma/fetched_date=2026-06-10/file.parquet"
                relative = key.removeprefix("silver/products/")
                local_path = local_cache_dir / relative
                local_path.parent.mkdir(parents=True, exist_ok=True)

                # Skip if local file exists with matching ETag (already fresh)
                if local_path.exists():
                    local_etag = _file_etag(local_path)
                    remote_etag = obj.get("ETag", "").strip('"')
                    if local_etag == remote_etag:
                        skipped += 1
                        continue

                # Download
                logger.debug("Downloading s3://%s/%s → %s", bucket, key, local_path)
                s3.download_file(bucket, key, str(local_path))
                downloaded += 1

    except (ClientError, NoCredentialsError) as exc:
        raise RuntimeError(f"R2 download failed: {exc}") from exc

    logger.info(
        "R2 Silver sync complete: %d downloaded, %d already cached.",
        downloaded, skipped,
    )

    return local_cache_dir


def _file_etag(path: Path) -> str:
    """Compute MD5 hex digest of a file — matches S3/R2 ETag for single-part uploads."""
    import hashlib
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


# ---------------------------------------------------------------------------
# Main matching runner
# ---------------------------------------------------------------------------

def run_matching(
    *,
    site_domain: str | None = None,
    target_date: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Run the full entity resolution pipeline.

    Parameters
    ----------
    site_domain:
        If provided, only process Silver records for this domain.
    target_date:
        If provided, only process Silver records from this date (YYYY-MM-DD).
    dry_run:
        If True, run all matching logic but skip all DB writes.
        Useful for testing and tuning thresholds.

    Returns
    -------
    dict
        Run statistics: total, matched_by_tier, new, flagged, errors.
    """
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Locate Silver data
    # ------------------------------------------------------------------
    project_root = Path(__file__).resolve().parent.parent.parent
    local_dev = os.getenv("R2_LOCAL_DEV_MODE", "False").lower() in ("true", "1")

    if local_dev:
        silver_root = project_root / "data" / "silver" / "products"
    else:
        # Production — download Parquet files from Cloudflare R2
        silver_root = _download_silver_from_r2(
            site_domain=site_domain,
            target_date=target_date,
            local_cache_dir=project_root / "data" / ".r2_cache" / "silver",
        )

    # ------------------------------------------------------------------
    # Load Silver records
    # ------------------------------------------------------------------
    df = _load_silver_records(silver_root, site_domain, target_date)
    if df.empty:
        logger.warning("No Silver records to process.")
        return {"total": 0}

    # Only process successful extractions with a name
    df = df[
        (df["extraction_method"] != "failed") &
        (df["raw_name"].fillna("") != "")
    ].copy()
    logger.info("Processing %d Silver records after filtering.", len(df))

    # ------------------------------------------------------------------
    # Load master catalogue index
    # ------------------------------------------------------------------
    idx = _build_master_index()

    # ------------------------------------------------------------------
    # Cache SiteConfig lookups
    # ------------------------------------------------------------------
    site_cache: dict[str, SiteConfig] = {}

    def _get_site(domain: str) -> SiteConfig | None:
        if domain not in site_cache:
            try:
                site_cache[domain] = SiteConfig.objects.get(domain=domain)
            except SiteConfig.DoesNotExist:
                logger.warning("No SiteConfig found for domain '%s'.", domain)
                site_cache[domain] = None
        return site_cache[domain]

    # ------------------------------------------------------------------
    # Run matching + write results
    # ------------------------------------------------------------------
    stats = {
        "total": len(df),
        "by_tier": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
        "new_masters": 0,
        "flagged_review": 0,
        "price_logs": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    new_masters_to_embed: list[tuple[UUID, str]] = []

    for batch_start in range(0, len(df), BATCH_SIZE):
        batch = df.iloc[batch_start : batch_start + BATCH_SIZE]
        logger.info(
            "Processing batch %d–%d / %d...",
            batch_start, min(batch_start + BATCH_SIZE, len(df)), len(df),
        )

        for _, row in batch.iterrows():
            try:
                match = _match_one(row, idx)
                stats["by_tier"][match.tier] += 1

                if match.needs_review:
                    stats["flagged_review"] += 1

                if dry_run:
                    continue

                domain = str(row.get("domain", "") or "")
                site = _get_site(domain)
                if site is None:
                    stats["errors"] += 1
                    continue

                with transaction.atomic():
                    master = _upsert_master_product(row, match, now)
                    sp = _upsert_site_product(row, master, site, match, now)
                    _insert_price_log(sp, master, site, row, now)
                    stats["price_logs"] += 1

                    if match.is_new:
                        stats["new_masters"] += 1
                        normalised = _normalise_name(str(row.get("raw_name", "")))
                        if normalised:
                            new_masters_to_embed.append((master.id, normalised))
                        # Add to in-memory index so subsequent rows can match against it
                        if row.get("raw_ean"):
                            idx.ean_index[str(row["raw_ean"])] = master.id
                        idx.name_index[normalised] = master.id
                        idx.all_names.append(normalised)
                        idx.all_ids.append(master.id)
                        # Register in domain_masters so same-site rows don't match it
                        domain = str(row.get("domain", "") or "")
                        if domain not in idx.domain_masters:
                            idx.domain_masters[domain] = set()
                        idx.domain_masters[domain].add(master.id)
                        # Register brand
                        if row.get("raw_brand"):
                            idx._master_brands[master.id] = _normalise_name(str(row["raw_brand"]))

            except Exception as exc:
                logger.error("Error processing row %s: %s", row.get("url"), exc)
                stats["errors"] += 1
                continue

    # ------------------------------------------------------------------
    # Store embeddings for newly created MasterProducts
    # ------------------------------------------------------------------
    if not dry_run and new_masters_to_embed:
        _store_embeddings(new_masters_to_embed)

    # ------------------------------------------------------------------
    # Log summary
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("Entity resolution complete%s.", " (DRY RUN)" if dry_run else "")
    logger.info("  Total records:      %d", stats["total"])
    logger.info("  Tier 1 (EAN):       %d", stats["by_tier"][1])
    logger.info("  Tier 2 (SKU):       %d", stats["by_tier"][2])
    logger.info("  Tier 3 (name):      %d", stats["by_tier"][3])
    logger.info("  Tier 3.5 (fp):      %d", stats["by_tier"][4])
    logger.info("  Tier 4 (vector):    %d", stats["by_tier"][5])
    logger.info("  Tier 6 (new):       %d", stats["by_tier"][6])
    logger.info("  New MasterProducts: %d", stats["new_masters"])
    logger.info("  Flagged for review: %d", stats["flagged_review"])
    logger.info("  Price logs written: %d", stats["price_logs"])
    logger.info("  Errors:             %d", stats["errors"])
    logger.info("=" * 60)

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Gold entity resolution.")
    parser.add_argument("--site", help="Only process this domain.")
    parser.add_argument("--date", help="Only process this date (YYYY-MM-DD).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run matching without writing to the database.",
    )
    args = parser.parse_args()

    run_matching(
        site_domain=args.site,
        target_date=args.date,
        dry_run=args.dry_run,
    )

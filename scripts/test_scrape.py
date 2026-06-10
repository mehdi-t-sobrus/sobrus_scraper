"""
scripts/test_scrape.py
======================
Quick test script — scrapes one or more product pages directly
without needing the discoverer, Redis queue, or Arq worker.

Usage
-----
    # Single URL
    python scripts/test_scrape.py https://universparadiscount.ma/cremes-depigmentantes/28730-...html

    # Multiple URLs
    python scripts/test_scrape.py \
        https://universparadiscount.ma/product-1.html \
        https://universparadiscount.ma/product-2.html

Output
------
Prints a clean summary of what was extracted from each page.
Also saves the raw result to /tmp/test_scrape_<timestamp>.json so you
can inspect the full HTML if needed.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap — load env and Django before importing worker
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "src" / "scrapers" / ".env")

import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

# ---------------------------------------------------------------------------
# Now safe to import worker internals
# ---------------------------------------------------------------------------
from scrapers.worker import (
    R2BronzeBuffer,
    _execute_request,
    _pick_proxy,
    _obfuscate_proxy,
    FetchStatus,
    FetchResult,
    IMPERSONATE_PROFILE,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
)
from urllib.parse import urlparse


async def scrape_url(url: str) -> dict:
    """Fetch a single URL and return the FetchResult dict."""
    domain = urlparse(url).netloc
    proxy = _pick_proxy(domain)
    proxy_label = _obfuscate_proxy(proxy) if proxy else None

    print(f"\n{'='*60}")
    print(f"URL    : {url}")
    print(f"Proxy  : {proxy_label or 'none (host IP)'}")
    print(f"Profile: {IMPERSONATE_PROFILE}")
    print(f"{'='*60}")

    start = time.monotonic()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            http_code, html, final_url, content_type = await asyncio.wait_for(
                _execute_request(url, proxy, timeout=REQUEST_TIMEOUT),
                timeout=REQUEST_TIMEOUT + 5,
            )
            elapsed = time.monotonic() - start

            print(f"HTTP   : {http_code}")
            print(f"Time   : {elapsed:.2f}s  (attempt {attempt})")
            print(f"Type   : {content_type}")
            print(f"Final  : {final_url}")
            print(f"HTML   : {len(html or ''):,} chars")

            result = FetchResult(
                url=url,
                status=FetchStatus.SUCCESS if 200 <= http_code < 300 else FetchStatus.HTTP_ERROR,
                html=html,
                http_status_code=http_code,
                final_url=final_url,
                content_type=content_type,
                content_length_bytes=len((html or "").encode("utf-8")),
                proxy_used=proxy_label,
                attempt_count=attempt,
                elapsed_seconds=round(elapsed, 3),
                fetched_at_utc=time.time(),
            )

            if 200 <= http_code < 300:
                _print_extracted(html or "")
            else:
                print(f"ERROR  : HTTP {http_code}")

            return result.to_dict()

        except asyncio.TimeoutError:
            print(f"TIMEOUT on attempt {attempt}/{MAX_RETRIES}")
            if attempt == MAX_RETRIES:
                return FetchResult(url=url, status=FetchStatus.TIMEOUT,
                                   error_message="Timeout").to_dict()
        except Exception as exc:
            print(f"ERROR  : {exc}")
            if attempt == MAX_RETRIES:
                return FetchResult(url=url, status=FetchStatus.UNKNOWN_ERROR,
                                   error_message=str(exc)).to_dict()

    return FetchResult(url=url, status=FetchStatus.UNKNOWN_ERROR).to_dict()


def _print_extracted(html: str) -> None:
    """Parse JSON-LD and key fields from the HTML and print a summary."""
    import re
    import json as _json
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)

    print(f"\n--- Extracted fields ---")

    # JSON-LD
    for script in tree.css("script[type='application/ld+json']"):
        raw = script.text(strip=True)
        if not raw:
            continue
        try:
            data = _json.loads(raw)
            entities = []
            if isinstance(data, dict):
                if data.get("@type") == "Product":
                    entities = [data]
                elif "@graph" in data:
                    entities = [e for e in data["@graph"] if e.get("@type") == "Product"]
            for e in entities:
                print(f"[JSON-LD]")
                print(f"  Name   : {e.get('name', '—')}")
                brand = e.get("brand", {})
                print(f"  Brand  : {brand.get('name', '—') if isinstance(brand, dict) else brand}")
                offers = e.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                print(f"  Price  : {offers.get('price', '—')} {offers.get('priceCurrency', '')}")
                print(f"  Stock  : {'InStock' if 'OutOfStock' not in offers.get('availability','') else 'OutOfStock'}")
                print(f"  Image  : {e.get('image', '—')}")
                desc = e.get("description", "")
                print(f"  Desc   : {desc[:120]}{'...' if len(desc) > 120 else ''}")
        except Exception:
            pass

    # CSS fallbacks
    h1 = tree.css_first("h1")
    if h1:
        print(f"[CSS] h1: {h1.text(strip=True)[:80]}")

    price_node = tree.css_first(".product-prices .price, .current-price .price, span.price")
    if price_node:
        print(f"[CSS] price: {price_node.text(strip=True)}")

    meta_price = tree.css_first("meta[property='product:price:amount']")
    if meta_price:
        print(f"[meta] price: {meta_price.attributes.get('content')} MAD")


async def main(urls: list[str]) -> None:
    results = []
    for url in urls:
        result = await scrape_url(url)
        results.append(result)

    # Save results (without html to keep it readable)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = Path(f"/tmp/test_scrape_{timestamp}.json")
    slim = [{k: v for k, v in r.items() if k != "html"} for r in results]
    out_path.write_text(json.dumps(slim, indent=2, default=str))
    print(f"\n{'='*60}")
    print(f"Results saved to {out_path} (html omitted for readability)")
    print(f"Scraped {len(results)} URL(s)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_scrape.py <url> [url2] [url3] ...")
        print("\nExample:")
        print("  python scripts/test_scrape.py \\")
        print("    'https://universparadiscount.ma/cremes-depigmentantes/28730-revalene-labs.html'")
        sys.exit(1)

    urls = sys.argv[1:]
    asyncio.run(main(urls))
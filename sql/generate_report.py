#!/usr/bin/env python3
"""
sql/generate_report.py
======================
Generates a self-contained HTML report from Silver Parquet data.
Output: sql/reports/silver_report_<date>.html

Usage (from repo root):
    # DEV — local Parquet files
    python sql/generate_report.py

    # PROD — Cloudflare R2
    export R2_ACCESS_KEY_ID=your_key
    export R2_SECRET_ACCESS_KEY=your_secret
    export R2_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
    python sql/generate_report.py --prod

Requirements (already in backend venv):
    pip install duckdb pandas
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

def get_connection(prod: bool = False):
    import duckdb
    conn = duckdb.connect()

    if prod:
        endpoint  = os.getenv("R2_ENDPOINT_URL", "")
        access_key = os.getenv("R2_ACCESS_KEY_ID", "")
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "")
        if not all([endpoint, access_key, secret_key]):
            print("ERROR: Set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY for prod mode.")
            sys.exit(1)
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute(f"SET s3_endpoint = '{endpoint}';")
        conn.execute(f"SET s3_access_key_id = '{access_key}';")
        conn.execute(f"SET s3_secret_access_key = '{secret_key}';")
        conn.execute("SET s3_region = 'auto';")
        conn.execute("SET s3_url_style = 'path';")
        silver_path = "s3://pipeline-silver/silver/products/**/*.parquet"
    else:
        silver_path = str(REPO_ROOT / "data" / "silver" / "products" / "**" / "*.parquet")

    conn.execute(f"CREATE OR REPLACE MACRO silver_path() AS '{silver_path}';")

    # Install silver_views
    views_sql = (REPO_ROOT / "sql" / "silver_views.sql").read_text()

    # Strip the prod R2 block and path config — already set above
    filtered_lines = []
    skip_next = False
    for line in views_sql.splitlines():
        stripped = line.strip()
        # Skip SET VARIABLE lines and R2 config — already handled
        if stripped.startswith("SET VARIABLE silver_path") or \
           stripped.startswith("INSTALL httpfs") or \
           stripped.startswith("LOAD httpfs") or \
           stripped.startswith("SET s3_"):
            continue
        # Skip commented prod lines
        if stripped.startswith("-- SET") or stripped.startswith("-- INSTALL") or stripped.startswith("-- LOAD"):
            continue
        filtered_lines.append(line)

    conn.execute("\n".join(filtered_lines))
    return conn


def df_to_html_table(df, table_id: str) -> str:
    """Convert a DataFrame to a styled HTML table."""
    if df.empty:
        return "<p style='color:#999;'>No data.</p>"

    headers = "".join(f"<th>{col}</th>" for col in df.columns)
    rows = ""
    for _, row in df.iterrows():
        cells = ""
        for val in row:
            if isinstance(val, float):
                formatted = f"{val:,.2f}" if val == val else "—"
            elif val is None:
                formatted = "—"
            else:
                formatted = str(val)
            cells += f"<td>{formatted}</td>"
        rows += f"<tr>{cells}</tr>"

    return f"""
    <div class="table-wrap">
        <table id="{table_id}">
            <thead><tr>{headers}</tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def build_html(sections: list[dict], generated_at: str, mode: str) -> str:
    """Build the complete self-contained HTML report."""

    section_nav = "\n".join(
        f'<li><a href="#{s["id"]}">{s["title"]}</a></li>'
        for s in sections
    )

    section_html = ""
    for s in sections:
        table_html = df_to_html_table(s["df"], s["id"] + "_table")
        section_html += f"""
        <section id="{s['id']}">
            <h2>{s['title']}</h2>
            <p class="desc">{s.get('desc', '')}</p>
            {table_html}
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Silver Pipeline Report — {generated_at}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         font-size: 14px; color: #1a1a1a; background: #f5f5f5; }}
  header {{ background: #185FA5; color: #fff; padding: 20px 32px;
            display: flex; justify-content: space-between; align-items: center; }}
  header h1 {{ font-size: 20px; font-weight: 600; }}
  header .meta {{ font-size: 12px; opacity: 0.8; text-align: right; }}
  .layout {{ display: flex; min-height: calc(100vh - 68px); }}
  nav {{ width: 220px; flex-shrink: 0; background: #fff; border-right: 1px solid #e0e0e0;
         padding: 20px 0; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
  nav ul {{ list-style: none; }}
  nav li a {{ display: block; padding: 8px 20px; color: #444; text-decoration: none;
              font-size: 13px; border-left: 3px solid transparent; }}
  nav li a:hover {{ background: #f0f7ff; color: #185FA5; border-left-color: #185FA5; }}
  main {{ flex: 1; padding: 28px 32px; max-width: calc(100% - 220px); overflow-x: auto; }}
  section {{ background: #fff; border-radius: 8px; padding: 20px 24px;
             margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  section h2 {{ font-size: 15px; font-weight: 600; color: #185FA5; margin-bottom: 6px; }}
  .desc {{ font-size: 12px; color: #888; margin-bottom: 14px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead tr {{ background: #f8f9fa; }}
  th {{ padding: 9px 12px; text-align: left; font-weight: 600; color: #555;
        border-bottom: 2px solid #dee2e6; white-space: nowrap; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #f0f0f0; white-space: nowrap; }}
  tr:hover td {{ background: #f8fbff; }}
  .badge-mode {{ display: inline-block; padding: 2px 10px; border-radius: 10px;
                font-size: 11px; font-weight: 600;
                background: {'#d4edda' if mode == 'dev' else '#cce5ff'};
                color: {'#155724' if mode == 'dev' else '#004085'}; }}
</style>
</head>
<body>
<header>
  <h1>🗂️ Silver Pipeline Report</h1>
  <div class="meta">
    Generated: {generated_at}<br>
    Source: <span class="badge-mode">{mode.upper()}</span>
  </div>
</header>
<div class="layout">
  <nav>
    <ul>
      {section_nav}
    </ul>
  </nav>
  <main>
    {section_html}
  </main>
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate Silver HTML report.")
    parser.add_argument("--prod", action="store_true", help="Read from Cloudflare R2 instead of local data/")
    args = parser.parse_args()

    mode = "prod" if args.prod else "dev"
    print(f"Generating Silver report ({mode} mode)...")

    conn = get_connection(prod=args.prod)
    import pandas as pd

    sections = [
        {
            "id": "overview",
            "title": "Overview",
            "desc": "Single-row health summary of the entire Silver layer.",
            "query": "SELECT * FROM silver_overview",
        },
        {
            "id": "by_site",
            "title": "Per-Site Breakdown",
            "desc": "Quality metrics per site — price coverage, EAN coverage, extraction methods.",
            "query": "SELECT * FROM silver_by_site",
        },
        {
            "id": "extraction_quality",
            "title": "Extraction Quality",
            "desc": "Which extraction strategy (JSON-LD, OG meta, CSS) fired for each site.",
            "query": "SELECT * FROM silver_extraction_quality ORDER BY domain, records DESC",
        },
        {
            "id": "cross_site_eans",
            "title": "Cross-Site Price Opportunities",
            "desc": "Products found on 2+ sites via EAN match — sorted by price spread (biggest saving first).",
            "query": "SELECT * FROM silver_cross_site_eans LIMIT 100",
        },
        {
            "id": "by_brand",
            "title": "Top Brands",
            "desc": "Brands ranked by number of listings, with price range and site coverage.",
            "query": "SELECT * FROM silver_by_brand LIMIT 50",
        },
        {
            "id": "price_bands",
            "title": "Price Distribution",
            "desc": "Products grouped into price bands per site.",
            "query": "SELECT * FROM silver_price_bands",
        },
        {
            "id": "recent",
            "title": "Latest Scrape Sample",
            "desc": "50 random products from the most recent scrape date per site.",
            "query": """
                SELECT domain, raw_name, raw_brand, raw_ean,
                       raw_price, raw_currency, in_stock, raw_category,
                       extraction_method, fetched_date
                FROM silver_recent
                USING SAMPLE 50
                ORDER BY domain, raw_brand, raw_name
            """,
        },
    ]

    for s in sections:
        print(f"  Querying: {s['title']}...")
        s["df"] = conn.execute(s["query"]).df()

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = build_html(sections, generated_at, mode)

    # Write output
    output_dir = REPO_ROOT / "sql" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"silver_report_{date_str}.html"
    output_path.write_text(html, encoding="utf-8")

    print(f"\n✅ Report generated: {output_path}")
    print(f"   Open in browser: open {output_path}")


if __name__ == "__main__":
    main()

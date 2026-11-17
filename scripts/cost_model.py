#!/usr/bin/env python3
"""Cost model: store and make searchable every released FOIA record, past and future.

Prints low / mid / high scenarios. Every input is an explicit, editable
assumption with its source or rationale in ASSUMPTIONS below. When a catalog
database with ingested FOIA logs is given (``--db``), pages-per-release is
recalibrated from the logs' own ``pages_released`` column.

Scope: third-party releases only (first-party records - A-files, medical,
personnel - are never public). Phase 2/3 (the system filing requests for
every requestable record) is out of scope.

    python scripts/cost_model.py            # scenarios as Markdown
    python scripts/cost_model.py --json     # machine-readable
    python scripts/cost_model.py --db data/foia_archive.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Dict

KB, TB = 1_000, 1_000_000_000_000

# name -> (low, mid, high, unit, rationale)
ASSUMPTIONS: Dict[str, tuple] = {
    "historic_released_requests": (
        2_000_000, 4_000_000, 7_000_000, "requests",
        "Third-party requests with a full/partial release, FY1998-FY2025. Government-wide processing "
        "rose from ~0.6-0.9M/yr (2008-2022) to 1.50M (FY2024) and ~1.64M (FY2025); most volume is "
        "first-party (DHS alone ~1.0M, mostly A-files). Pre-digital releases mostly exist only on paper.",
    ),
    "future_released_requests_per_year": (
        150_000, 250_000, 400_000, "requests/yr",
        "Third-party share of ~1.6M processed/yr with records released in full or part.",
    ),
    "pages_per_release": (
        30, 120, 400, "pages",
        "Not reported government-wide. Heavy-tailed (most releases are small; litigation productions "
        "run to thousands). Recalibrated from logs' pages_released when --db is given.",
    ),
    "bytes_per_page": (
        40 * KB, 100 * KB, 250 * KB, "bytes",
        "Redacted releases are usually flattened image PDFs (~50-150 KB/page); native text is smaller, "
        "color scans larger. Archive median file in the Nov 2026 crawl: 422 KB.",
    ),
    "share_needing_ocr": (
        0.5, 0.7, 0.9, "fraction",
        "Redaction normally removes the text layer, so most released pages need OCR.",
    ),
    "text_bytes_per_page": (
        1_500, 2_500, 4_000, "bytes", "Extracted plain text per page.",
    ),
    "index_overhead": (
        1.5, 2.0, 3.0, "x text", "Full-text index size relative to raw text (SQLite FTS5/Postgres/OpenSearch).",
    ),
}

PRICES = {
    # $ per TB-month / per 1,000 pages. Secondary sources, Nov 2026; verify before budgeting.
    "b2_tb_month": 6.95,             # Backblaze B2 pay-as-you-go (raised from $6.00 in 2026)
    "r2_tb_month": 15.00,            # Cloudflare R2 Standard, free egress
    "deep_archive_tb_month": 0.99,   # AWS S3 Glacier Deep Archive (second copy, 12-48 h restore)
    "ocr_cloud_per_1000": 1.50,      # Google Document AI OCR / Textract / Azure Read
    "ocr_self_hosted_per_1000": 0.05,  # Tesseract on rented CPU, ~2 s/page at ~$0.05/core-hour
    "search_server_month": (60, 150, 400),  # dedicated NVMe server for the text index, by scenario
}


def calibrate_pages(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        rows = [r[0] for r in conn.execute(
            "SELECT pages_released FROM closed_requests WHERE releasable=1 AND pages_released > 0")]
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if len(rows) < 100:
        return {"n": len(rows), "note": "fewer than 100 logged releases with page counts; not used"}
    rows.sort()
    mean = sum(rows) / len(rows)
    return {"n": len(rows), "mean": round(mean, 1), "median": rows[len(rows) // 2],
            "p90": rows[int(len(rows) * 0.9)]}


def scenario(i: int, pages_override=None) -> Dict[str, float]:
    a = {k: v[i] for k, v in ASSUMPTIONS.items()}
    if pages_override:
        a["pages_per_release"] = pages_override
    hist_pages = a["historic_released_requests"] * a["pages_per_release"]
    year_pages = a["future_released_requests_per_year"] * a["pages_per_release"]
    hist_tb = hist_pages * a["bytes_per_page"] / TB
    year_tb = year_pages * a["bytes_per_page"] / TB
    index_tb = hist_pages * a["text_bytes_per_page"] * (1 + a["index_overhead"]) / TB
    index_year_tb = year_pages * a["text_bytes_per_page"] * (1 + a["index_overhead"]) / TB
    ocr_hist = hist_pages * a["share_needing_ocr"] / 1000
    ocr_year = year_pages * a["share_needing_ocr"] / 1000
    storage_month = hist_tb * (PRICES["b2_tb_month"] + PRICES["deep_archive_tb_month"])
    growth_month = year_tb * (PRICES["b2_tb_month"] + PRICES["deep_archive_tb_month"])
    server = PRICES["search_server_month"][i]
    return {
        **{f"in_{k}": v for k, v in a.items()},
        "historic_pages": hist_pages,
        "historic_originals_tb": hist_tb,
        "historic_index_tb": index_tb,
        "future_pages_per_year": year_pages,
        "future_originals_tb_per_year": year_tb,
        "future_index_tb_per_year": index_year_tb,
        "one_time_ocr_cloud_usd": ocr_hist * PRICES["ocr_cloud_per_1000"],
        "one_time_ocr_self_hosted_usd": ocr_hist * PRICES["ocr_self_hosted_per_1000"],
        "yearly_ocr_cloud_usd": ocr_year * PRICES["ocr_cloud_per_1000"],
        "yearly_ocr_self_hosted_usd": ocr_year * PRICES["ocr_self_hosted_per_1000"],
        "storage_usd_per_month_at_full_backfill": storage_month,
        "storage_growth_usd_per_month_each_year": growth_month,
        "search_server_usd_per_month": server,
        "year1_total_self_hosted_ocr_usd": ocr_hist * PRICES["ocr_self_hosted_per_1000"]
        + ocr_year * PRICES["ocr_self_hosted_per_1000"] + 12 * (storage_month + server) + 6 * growth_month,
        "year1_total_cloud_ocr_usd": ocr_hist * PRICES["ocr_cloud_per_1000"]
        + ocr_year * PRICES["ocr_cloud_per_1000"] + 12 * (storage_month + server) + 6 * growth_month,
    }


def _fmt(v):
    if isinstance(v, float) and v < 100:
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="catalog SQLite with closed_requests (recalibrates pages)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    calibration = calibrate_pages(args.db) if args.db else None
    override = None
    if calibration and "mean" in calibration:
        override = calibration["mean"]
    names = ("low", "mid", "high")
    results = {n: scenario(i, override if n == "mid" else None) for i, n in enumerate(names)}
    if args.json:
        print(json.dumps({"scenarios": results, "prices": PRICES, "calibration": calibration,
                          "assumptions": {k: {"low": v[0], "mid": v[1], "high": v[2], "unit": v[3],
                                              "why": v[4]} for k, v in ASSUMPTIONS.items()}},
                         indent=2, sort_keys=True))
        return
    keys = [k for k in results["mid"] if not k.startswith("in_")]
    print("| Output | Low | Mid | High |\n|---|---:|---:|---:|")
    for k in keys:
        print(f"| {k} | " + " | ".join(_fmt(results[n][k]) for n in names) + " |")
    if calibration:
        print(f"\nCalibration from logs: {calibration}")


if __name__ == "__main__":
    main()

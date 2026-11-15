"""FC-12 completeness denominators and FC-14 broad-corpus review evidence.

FC-12 reports *distinct* denominators — sources known / verified / enumerated
/ blocked / unknown, records cataloged / archived / remote-only, bytes, text
indexed, OCR — by agency, year and source type, plus explicit unknowns. It
never substitutes data volume for federal coverage, and the total number of
public FOIA records is reported as unknowable rather than estimated.

FC-14 assembles a stratified review with gate checks. It is evidence for an
owner decision: ``downstream_unblocked`` is always false here, and review
status stays ``draft`` until a human accepts it elsewhere.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .source_inventory import build_source_inventory

UNKNOWABLE = ("The total number of public federal FOIA records is not knowable; "
              "no completeness percentage of all records is reported.")
INDEXED = "('indexed','indexed_truncated')"


def _has(conn, table) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _rows(conn, sql, params=()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def compute_completeness(conn: sqlite3.Connection, census: Optional[Dict[str, Any]] = None,
                         *, now: Optional[datetime] = None) -> Dict[str, Any]:
    inventory = build_source_inventory(census, conn=conn)
    inv = inventory["summary"]
    records = conn.execute(
        f"""SELECT COUNT(*) AS cataloged,
                   COALESCE(SUM(d.download_status='downloaded'),0) AS archived,
                   COALESCE(SUM(CASE WHEN d.download_status='downloaded' THEN d.file_size END),0) AS archived_bytes,
                   COALESCE(SUM(t.extraction_status IN {INDEXED}),0) AS text_indexed,
                   COALESCE(SUM(t.extraction_status IN {INDEXED} AND t.extraction_method LIKE '%ocr%'),0) AS ocr_indexed,
                   COALESCE(SUM(d.published_date IS NULL OR d.published_date=''),0) AS without_published_date
            FROM documents d LEFT JOIN document_text t ON t.document_id=d.id WHERE d.url IS NOT NULL"""
    ).fetchone()
    records = dict(records)
    records["metadata_only"] = records["cataloged"] - records["archived"]
    acquisition = _rows(conn, """SELECT COALESCE(download_status,'unknown') AS status, COUNT(*) AS records
                                 FROM documents GROUP BY 1 ORDER BY 2 DESC""")
    extraction = _rows(conn, """SELECT COALESCE(t.extraction_status,'not_attempted') AS status, COUNT(*) AS records
                                FROM documents d LEFT JOIN document_text t ON t.document_id=d.id
                                GROUP BY 1 ORDER BY 2 DESC""")
    by_agency = _rows(conn, f"""
        SELECT COALESCE(a.name, 'Unattributed') AS agency, COUNT(*) AS cataloged,
               COALESCE(SUM(d.download_status='downloaded'),0) AS archived,
               COALESCE(SUM(t.extraction_status IN {INDEXED}),0) AS text_indexed,
               MIN(NULLIF(substr(d.published_date,1,4),'')) AS oldest_year,
               MAX(NULLIF(substr(d.published_date,1,4),'')) AS newest_year
        FROM documents d LEFT JOIN agencies a ON a.id=d.agency_id
        LEFT JOIN document_text t ON t.document_id=d.id
        GROUP BY 1 ORDER BY cataloged DESC, agency""")
    by_year = _rows(conn, """
        SELECT CASE WHEN substr(published_date,1,4) GLOB '[12][0-9][0-9][0-9]' THEN substr(published_date,1,4)
                    ELSE 'unknown' END AS year,
               COUNT(*) AS cataloged, COALESCE(SUM(download_status='downloaded'),0) AS archived
        FROM documents GROUP BY 1 ORDER BY 1""")
    by_source_type = _rows(conn, """
        SELECT COALESCE(rr.source_type,'unknown') AS source_type, COUNT(DISTINCT rr.id) AS sources,
               COUNT(DISTINCT ds.document_id) AS records,
               COALESCE(SUM(rr.last_successful_crawl_at IS NOT NULL),0) AS source_rows_with_success
        FROM reading_rooms rr LEFT JOIN document_sources ds ON ds.reading_room_id=rr.id
        GROUP BY 1 ORDER BY 2 DESC""")
    crawl = conn.execute("""SELECT MAX(last_successful_crawl_at) AS last_verified_crawl,
                                   COALESCE(SUM(last_successful_crawl_at IS NULL),0) AS never_successfully_crawled,
                                   COUNT(*) AS database_sources FROM reading_rooms""").fetchone()
    result = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "sources": {
            "known": inv["sources"], "official": inv["official_sources"],
            "third_party": inv["third_party_sources"],
            "access_status": inv["access_status_counts"],
            "verified_active": inv["access_status_counts"]["ACTIVE"],
            "unknown_or_unverified": inv["access_status_counts"]["UNVERIFIED"],
            "blocked": inv["access_status_counts"]["BLOCKED"],
            "enumeration_state": inv["enumeration_state_counts"],
            "fully_enumerated": inv["enumeration_state_counts"]["EXHAUSTED"],
            "database_sources": crawl["database_sources"],
            "database_sources_not_in_inventory": inv["database_sources_not_in_inventory"],
            "never_successfully_crawled": crawl["never_successfully_crawled"],
            "last_verified_crawl": crawl["last_verified_crawl"],
            "by_source_type": by_source_type,
        },
        "intelligence_community": {"expected": inv["ic_elements_expected"],
                                   "with_source": inv["ic_elements_with_source"],
                                   "missing": inv["ic_elements_missing"]},
        "records": records,
        "acquisition_status": acquisition,
        "extraction_status": extraction,
        "by_agency": by_agency,
        "by_year": by_year,
        "unknowns": {"total_public_foia_records": None, "note": UNKNOWABLE,
                     "records_without_published_date": records["without_published_date"],
                     "sources_unverified": inv["access_status_counts"]["UNVERIFIED"]},
    }
    if _has(conn, "document_relations"):
        from .identity import relation_summary
        result["dedupe"] = relation_summary(conn)
    if _has(conn, "request_logs"):
        from .request_logs import request_log_summary
        result["request_logs"] = request_log_summary(conn)
    return result


GATES = (
    ("many_sources_enumerated", "At least 25 sources fully enumerated (FC-02)"),
    ("ic_coverage", "All 18 IC elements have a recognized source (FC-01)"),
    ("multi_agency", "Records from at least 20 agencies"),
    ("multi_year", "Records spanning at least 10 published years"),
    ("third_party_present", "Third-party releases ingested and crosslinked (FC-04/FC-08)"),
    ("archive_hashes", "Every archived record has a SHA-256"),
    ("request_logs_present", "Official request logs normalized (FC-03)"),
    ("recovery_evidence", "Latest ingestion run has a VERIFIED checkpoint"),
)


def build_corpus_review(conn: sqlite3.Connection, census: Optional[Dict[str, Any]] = None,
                        *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """FC-14 stratified review. Gates report pass/fail/unknown from evidence only."""
    c = compute_completeness(conn, census, now=now)
    years = [r for r in c["by_year"] if r["year"] != "unknown"]
    agencies = [r for r in c["by_agency"] if r["agency"] != "Unattributed"]
    archived_without_hash = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE download_status='downloaded' AND (sha256 IS NULL OR sha256='')"
    ).fetchone()[0]
    third_party = conn.execute("SELECT COUNT(*) FROM third_party_releases").fetchone()[0] \
        if _has(conn, "third_party_releases") else None
    checkpoint = None
    if _has(conn, "ingestion_runs"):
        row = conn.execute("SELECT checkpoint_state FROM ingestion_runs ORDER BY updated_at DESC LIMIT 1").fetchone()
        checkpoint = row[0] if row else None
    status = {
        "many_sources_enumerated": c["sources"]["fully_enumerated"] >= 25,
        "ic_coverage": not c["intelligence_community"]["missing"],
        "multi_agency": len(agencies) >= 20,
        "multi_year": len(years) >= 10,
        "third_party_present": None if third_party is None else third_party > 0,
        "archive_hashes": c["records"]["archived"] > 0 and archived_without_hash == 0,
        "request_logs_present": None if "request_logs" not in c else c["request_logs"]["logs"] > 0,
        "recovery_evidence": None if checkpoint is None else checkpoint == "VERIFIED",
    }
    gates = [{"id": gid, "description": desc,
              "result": "unknown" if status[gid] is None else ("pass" if status[gid] else "fail")}
             for gid, desc in GATES]
    strata = {
        "agency": c["by_agency"],
        "period": c["by_year"],
        "source_type": c["sources"]["by_source_type"],
        "provenance": {"official_records": c["records"]["cataloged"], "third_party_releases": third_party},
        "document_type": _rows(conn, """SELECT COALESCE(NULLIF(lower(file_type),''),'unknown') AS file_type,
                                        COUNT(*) AS records, COALESCE(SUM(download_status='downloaded'),0) AS archived
                                        FROM documents GROUP BY 1 ORDER BY 2 DESC"""),
        "extraction_method": _rows(conn, """SELECT COALESCE(extraction_method,'none') AS method, COUNT(*) AS records
                                            FROM document_text GROUP BY 1 ORDER BY 2 DESC"""),
        "disclosure_status": _rows(conn, """SELECT COALESCE(disposition,'unknown') AS disposition, COUNT(*) AS requests
                                            FROM request_log_rows GROUP BY 1 ORDER BY 2 DESC""")
        if _has(conn, "request_log_rows") else [],
        "intelligence_community": c["intelligence_community"],
    }
    return {
        "review_version": "fc14-corpus-review/1",
        "review_status": "draft",
        "downstream_unblocked": False,
        "generated_at": c["generated_at"],
        "gates": gates,
        "all_gates_pass": all(g["result"] == "pass" for g in gates),
        "missing_known_files": {"metadata_only": c["records"]["metadata_only"],
                                "acquisition_status": c["acquisition_status"]},
        "blocked_sources": c["sources"]["blocked"],
        "strata": strata,
        "unknowns": c["unknowns"],
        "note": ("Evidence for an owner decision. Passing gates does not certify nationwide coverage; "
                 "acceptance and downstream unblocking are recorded outside this generator."),
    }

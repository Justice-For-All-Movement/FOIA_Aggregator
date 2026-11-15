"""Additive, parallel export contracts next to ``foia-catalog-export/1``.

* ``foia-document-text-export/1`` — extracted text per released document with
  its SHA-256 and extraction provenance, linked by the FC-13 ``record_id``.
* ``foia-request-log-export/1`` — FC-03 request-log rows with row-level
  provenance. Requester names/organizations and raw cells are never exported.
* ``foia-log-coverage/1`` — per agency/component/year request-log coverage in
  the manifest/record shape of Send_FOIA_Log_Requests'
  ``sfl-log-coverage-proposal-1`` (same field sets), so that consumer only has
  to accept the producer's version string.

None of these change FC-13 records or its pinned schema. Every manifest names
the producer commit (see ``producer.py``); an unpinned build reports ``null``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .export_contract import ExportCursorError, decode_cursor, encode_cursor, record_id_for
from .producer import producer_info

TEXT_VERSION = "foia-document-text-export/1"
LOG_ROW_VERSION = "foia-request-log-export/1"
COVERAGE_CONTRACT = "foia-aggregator-export"
COVERAGE_VERSION = "foia-log-coverage/1"
COVERAGE_COMPATIBLE_WITH = "sfl-log-coverage-proposal-1"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
SCHEMAS = {
    "document-text.v1": "document-text.v1.schema.json",
    "request-log-row.v1": "request-log-row.v1.schema.json",
    "log-coverage-record.v1": "log-coverage-record.v1.schema.json",
    "log-coverage-manifest.v1": "log-coverage-manifest.v1.schema.json",
}
TEXT_MAX_PAGE = 100
TEXT_PAGE_BYTE_BUDGET = 8 * 1024 * 1024
LOG_ROW_MAX_PAGE = 1000
INDEXED = ("indexed", "indexed_truncated")
LOG_ROW_EXCLUDED = ["requester_name", "requester_organization", "raw_cells"]
TEXT_EXCLUDED = ["storage_key", "local_path", "storage_backend", "download_error"]
COVERAGE_REQUIRED_FIELDS = ("tracking_number", "request_description", "received_date",
                            "closed_date", "disposition")
GAP_DAYS = 31


def load_extension_schema(name: str) -> Dict[str, Any]:
    if name not in SCHEMAS:
        raise KeyError(name)
    return json.loads((SCHEMA_DIR / SCHEMAS[name]).read_text(encoding="utf-8"))


def _fingerprint(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, separators=(",", ":"), default=str)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# -- document text -------------------------------------------------------------

def text_snapshot_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        f"""SELECT COUNT(*), COALESCE(MAX(document_id),0), COALESCE(SUM(character_count),0),
                   COALESCE(SUM(LENGTH(body)),0)
            FROM document_text WHERE extraction_status IN {INDEXED}"""
    ).fetchone()
    return _fingerprint("tsnap_", [TEXT_VERSION, *tuple(row)])


def export_text_page(conn: sqlite3.Connection, *, cursor: Optional[str] = None,
                     limit: int = 20) -> Dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= TEXT_MAX_PAGE:
        raise ValueError(f"limit must be 1..{TEXT_MAX_PAGE}")
    snapshot_id = text_snapshot_id(conn)
    after = decode_cursor(cursor, snapshot_id) if cursor else 0
    rows = conn.execute(
        f"""SELECT d.id, d.url, d.sha256, d.download_status, t.body, t.extraction_status,
                   t.extraction_method, t.character_count, t.truncated
            FROM document_text t JOIN documents d ON d.id = t.document_id
            WHERE t.extraction_status IN {INDEXED} AND d.url IS NOT NULL AND d.id > ?
            ORDER BY d.id LIMIT ?""",
        (after, limit + 1),
    ).fetchall()
    records, used, last_id = [], 0, None
    for row in rows[:limit]:
        body = row["body"] or ""
        size = len(body.encode("utf-8"))
        if records and used + size > TEXT_PAGE_BYTE_BUDGET:
            break  # keep pages bounded; the cursor resumes here
        archived = row["download_status"] == "downloaded"
        records.append({
            "record_id": record_id_for(row["url"]),
            "url": row["url"],
            "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "text": body,
            "character_count": row["character_count"],
            "truncated": bool(row["truncated"]),
            "extraction": {"status": row["extraction_status"], "method": row["extraction_method"]},
            "source_document": {
                "preservation_status": "archived" if archived else "metadata_only",
                "sha256": row["sha256"] if archived else None,
            },
        })
        used += size
        last_id = row["id"]
    more = len(records) < len(rows)
    return {"schema_version": TEXT_VERSION, "snapshot_id": snapshot_id, "records": records,
            "next_cursor": encode_cursor(last_id, snapshot_id) if more and last_id is not None else None}


# -- request-log rows ------------------------------------------------------------

def log_rows_snapshot_id(conn: sqlite3.Connection) -> str:
    if not _has_table(conn, "request_log_rows"):
        return _fingerprint("lsnap_", [LOG_ROW_VERSION, 0, 0])
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM request_log_rows").fetchone()
    logs = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
    return _fingerprint("lsnap_", [LOG_ROW_VERSION, *tuple(row), logs])


def log_row_id(log_id: str, sheet: Optional[str], row_number: int) -> str:
    return "lrow_" + hashlib.sha256(f"{log_id}\x00{sheet or ''}\x00{row_number}".encode()).hexdigest()[:24]


def export_log_rows_page(conn: sqlite3.Connection, *, cursor: Optional[str] = None,
                         limit: int = 200) -> Dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= LOG_ROW_MAX_PAGE:
        raise ValueError(f"limit must be 1..{LOG_ROW_MAX_PAGE}")
    snapshot_id = log_rows_snapshot_id(conn)
    after = decode_cursor(cursor, snapshot_id) if cursor else 0
    if not _has_table(conn, "request_log_rows"):
        return {"schema_version": LOG_ROW_VERSION, "snapshot_id": snapshot_id, "records": [], "next_cursor": None}
    rows = conn.execute(
        """SELECT r.rowid AS rid, r.*, l.source_url, l.raw_sha256, l.agency, l.file_format
           FROM request_log_rows r JOIN request_logs l USING(log_id)
           WHERE r.rowid > ? ORDER BY r.rowid LIMIT ?""",
        (after, limit + 1),
    ).fetchall()
    records = []
    for row in rows[:limit]:
        confidence = {k: v for k, v in json.loads(row["field_confidence_json"]).items()
                      if k not in ("requester_name", "requester_organization")}
        records.append({
            "row_id": log_row_id(row["log_id"], row["sheet"], row["row_number"]),
            "log": {"log_id": row["log_id"], "source_url": row["source_url"], "sha256": row["raw_sha256"],
                    "format": row["file_format"], "agency": row["agency"]},
            "sheet": row["sheet"],
            "row_number": row["row_number"],
            "tracking_number": row["tracking_number"],
            "request_description": row["request_description"],
            "received_date": row["received_date"],
            "closed_date": row["closed_date"],
            "disposition": row["disposition"],
            "component": row["component"],
            "exemptions": row["exemptions"],
            "released_record_reference": row["released_record_reference"],
            "field_confidence": confidence,
            "warnings": json.loads(row["warnings_json"]),
        })
    more = len(rows) > limit
    return {"schema_version": LOG_ROW_VERSION, "snapshot_id": snapshot_id, "records": records,
            "next_cursor": encode_cursor(rows[limit - 1]["rid"], snapshot_id) if more else None}


# -- per agency/year log coverage ------------------------------------------------

def _utc_z(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gaps(start: date, end: date, dates: List[date]) -> List[List[str]]:
    """Spans longer than GAP_DAYS with no received request, inside [start, end]."""
    gaps, cursor = [], start
    for current in sorted(set(dates)) + [end + timedelta(days=1)]:
        if (current - cursor).days > GAP_DAYS:
            gaps.append([cursor.isoformat(), (current - timedelta(days=1)).isoformat()])
        cursor = max(cursor, current + timedelta(days=1))
    return gaps


def build_log_coverage(conn: sqlite3.Connection, *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Coverage per (log, agency, component, calendar year of received_date).

    ``availability`` is ``full`` only when the year is over, the log maps every
    required field, and no >31-day span lacks a received request; otherwise
    ``partial``. Rows without a parseable received date cannot be placed in a
    year and are not counted as coverage.
    """
    if not _has_table(conn, "request_logs"):
        return []
    today = today or datetime.now(timezone.utc).date()
    groups: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT l.log_id, l.source_url, l.raw_sha256, l.agency, l.column_mapping_json, l.ingested_at,
                  r.component, r.received_date
           FROM request_log_rows r JOIN request_logs l USING(log_id)
           WHERE r.received_date IS NOT NULL ORDER BY l.log_id"""
    ):
        try:
            received = date.fromisoformat(row["received_date"])
        except ValueError:
            continue
        key = (row["log_id"], (row["component"] or "unspecified").strip() or "unspecified", received.year)
        group = groups.setdefault(key, {"row": row, "dates": []})
        group["dates"].append(received)
    records = []
    for (log_id, component, year), group in sorted(groups.items()):
        row = group["row"]
        start, end = date(year, 1, 1), date(year, 12, 31)
        mapped = set(json.loads(row["column_mapping_json"]))
        missing = [f for f in COVERAGE_REQUIRED_FIELDS if f not in mapped]
        gaps = _gaps(start, end, group["dates"])
        full = end < today and not missing and not gaps
        records.append({
            "coverage_id": "cov_" + hashlib.sha256(f"{log_id}\x00{component}\x00{year}".encode()).hexdigest()[:20],
            "jurisdiction": "us-federal",
            "agency_abbr": (row["agency"] or "unknown"),
            "component_abbr": component,
            "record_class": "foia_request_log",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "availability": "full" if full else "partial",
            "artifact_sha256": row["raw_sha256"],
            "source_url": row["source_url"],
            "retrieved_at": _utc_z(row["ingested_at"]),
            "missing_fields": missing,
            "date_gaps": gaps,
            "synthetic": False,
        })
    return records


def log_coverage_files(conn: sqlite3.Connection, *, fixture_mode: bool = False,
                       now: Optional[datetime] = None, today: Optional[date] = None,
                       env=None) -> Tuple[bytes, bytes]:
    """Return (manifest.json bytes, coverage.jsonl bytes) in the SFL-compatible shape."""
    records = build_log_coverage(conn, today=today)
    if fixture_mode:
        for record in records:
            record["synthetic"] = True
    body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    info = producer_info(env) if env is not None else producer_info()
    producer = ({"name": "synthetic-fixture", "commit": "synthetic"} if fixture_mode
                else {"name": info["name"], "commit": info["commit"]})
    manifest = {
        "contract": COVERAGE_CONTRACT,
        "contract_version": COVERAGE_VERSION,
        "dataset": "log_coverage",
        "snapshot_id": "logcov_" + hashlib.sha256(body).hexdigest()[:20],
        "producer": producer,
        "observed_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture_mode": fixture_mode,
        "fc14_review": {"status": "absent", "reference": None},
        "records": {"path": "coverage.jsonl", "sha256": hashlib.sha256(body).hexdigest(), "count": len(records)},
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"), body


def write_log_coverage(conn, output_dir: Path, **kwargs) -> Dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest, body = log_coverage_files(conn, **kwargs)
    (output_dir / "coverage.jsonl").write_bytes(body)
    (output_dir / "manifest.json").write_bytes(manifest)
    return json.loads(manifest)


# -- generic snapshot writer for paged datasets ------------------------------------

DATASETS: Dict[str, Tuple[str, Callable[..., Dict[str, Any]], int, str]] = {
    "document-text": (TEXT_VERSION, export_text_page, TEXT_MAX_PAGE, "record_id"),
    "request-log-rows": (LOG_ROW_VERSION, export_log_rows_page, LOG_ROW_MAX_PAGE, "row_id"),
}


def write_dataset_snapshot(conn: sqlite3.Connection, output_dir: Path, dataset: str, *,
                           now: Optional[datetime] = None, env=None) -> Dict[str, Any]:
    version, pager, page_size, id_field = DATASETS[dataset]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    files, cursor, total, snapshot = [], None, 0, None
    while True:
        page = pager(conn, cursor=cursor, limit=page_size)
        if snapshot is None:
            snapshot = page["snapshot_id"]
        elif page["snapshot_id"] != snapshot:
            raise ExportCursorError("Archive changed during export; rerun against a stable snapshot")
        if page["records"]:
            name = f"{dataset}-{len(files) + 1:05d}.jsonl"
            body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in page["records"])
            (output_dir / name).write_text(body, encoding="utf-8")
            files.append({"path": name, "records": len(page["records"]),
                          "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
            total += len(page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    manifest = {
        "schema_version": version, "dataset": dataset, "snapshot_id": snapshot,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "producer": producer_info(env) if env is not None else producer_info(),
        "id_field": id_field, "counts": {"records": total}, "files": files,
        "excluded_fields": LOG_ROW_EXCLUDED if dataset == "request-log-rows" else TEXT_EXCLUDED,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest

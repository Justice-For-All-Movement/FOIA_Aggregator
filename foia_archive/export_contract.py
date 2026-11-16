"""FC-13: versioned, research-ready catalog export contract.

``foia-catalog-export/1`` records have stable IDs derived from the canonical
document URL (SQLite surrogate IDs can change between restored snapshots),
explicit official-source provenance, a preservation block that keeps
metadata-only remote pointers distinct from archived binaries, and text-index
status. Internal storage locations (B2 keys, local paths), download error text
and any personal request-log fields are never exported. Nothing is enriched
synthetically: every field comes from the archive database as stored.

Pagination uses an opaque cursor bound to a snapshot fingerprint so a client
cannot silently mix pages from two different archive states.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .producer import producer_info
from .source_inventory import normalize_url, source_id_for

SCHEMA_VERSION = "foia-catalog-export/1"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "catalog-record.v1.schema.json"
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100
LICENSE_NOTE = (
    "Records are U.S. federal government releases obtained from public sources; "
    "catalog metadata is provided as-is. Check each record's source for any "
    "third-party rights. Remote-only records are pointers, not preserved copies."
)


class ExportCursorError(ValueError):
    """Malformed cursor, or a cursor from a different archive snapshot."""


def record_id_for(url: str) -> str:
    return "rec_" + hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:24]


def snapshot_fingerprint(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS max_id,
                  COALESCE(SUM(download_status='downloaded'),0) AS archived,
                  COALESCE(MAX(downloaded_at),'') AS last_download,
                  COALESCE(MAX(discovered_at),'') AS last_discovery
           FROM documents"""
    ).fetchone()
    text_rows = conn.execute("SELECT COUNT(*) FROM document_text").fetchone()[0]
    payload = json.dumps([SCHEMA_VERSION, *tuple(row), text_rows], separators=(",", ":"))
    return "snap_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def encode_cursor(after_id: int, snapshot_id: str) -> str:
    raw = json.dumps({"a": after_id, "s": snapshot_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, snapshot_id: str) -> int:
    if not isinstance(cursor, str) or len(cursor) > 200:
        raise ExportCursorError("Invalid cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        after_id, snapshot = data["a"], data["s"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise ExportCursorError("Invalid cursor") from None
    if type(after_id) is not int or after_id < 0 or not isinstance(snapshot, str):
        raise ExportCursorError("Invalid cursor")
    if snapshot != snapshot_id:
        raise ExportCursorError("Cursor belongs to a different archive snapshot; restart the export")
    return after_id


def _year(published: Optional[str]) -> Optional[int]:
    if published and len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


def _record(row: sqlite3.Row, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    archived = row["download_status"] == "downloaded"
    indexed = row["extraction_status"] in ("indexed", "indexed_truncated")
    return {
        "record_id": record_id_for(row["url"]),
        "url": row["url"],
        "title": row["title"],
        "description": row["description"],
        "filename": row["filename"],
        "file_type": row["file_type"],
        "mime_type": row["mime_type"],
        "published_date": row["published_date"],
        "published_year": _year(row["published_date"]),
        "discovered_at": row["discovered_at"],
        "agency": {"slug": row["agency_slug"], "name": row["agency_name"]} if row["agency_slug"] else None,
        "office": {"slug": row["office_slug"], "name": row["office_name"]} if row["office_slug"] else None,
        "sources": sources,
        "preservation": {
            "status": "archived" if archived else "metadata_only",
            "acquisition_status": row["download_status"] or "unknown",
            "sha256": row["sha256"] if archived else None,
            "file_size": row["file_size"] if archived else None,
            "archived_at": row["downloaded_at"] if archived else None,
        },
        "text": {
            "indexed": indexed,
            "extraction_status": row["extraction_status"],
            "extraction_method": row["extraction_method"],
            "character_count": row["character_count"],
            "truncated": bool(row["truncated"]) if row["truncated"] is not None else None,
        },
    }


def _iso_day(value: Optional[str], label: str) -> Optional[str]:
    if value is None or value == "":
        return None
    text = str(value)
    try:
        if len(text) != 10:
            raise ValueError
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be YYYY-MM-DD") from None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _record_id(row) -> str:
    return record_id_for(row["url"])


def _in_range(value: Optional[str], since: Optional[str], until: Optional[str]) -> bool:
    if not value:
        return False
    day = str(value)[:10]
    return (not since or day >= since) and (not until or day <= until)


def _match_basis(row, since: Optional[str], until: Optional[str]) -> Dict[str, Any]:
    keys = row.keys()
    source = row["published_date_source"] if "published_date_source" in keys else None
    newly = row["newly_found_at"] if "newly_found_at" in keys else None
    if _in_range(row["published_date"], since, until):
        return {"basis": "dated", "date": str(row["published_date"])[:10], "date_source": source or "page"}
    return {"basis": "newly_found", "date": str(newly)[:10] if newly else None, "date_source": "first_seen"}


def export_page(
    conn: sqlite3.Connection, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE_SIZE,
    published_since: Optional[str] = None, published_until: Optional[str] = None,
    basis: str = "any",
) -> Dict[str, Any]:
    """One page of catalog records (id order, snapshot-bound cursor).

    ``published_since``/``published_until`` (inclusive YYYY-MM-DD) restrict
    the page by ``basis``: "dated" (release date in range), "new" (first seen
    after the source's baseline crawl, in range) or "any" (either; default).
    Records with neither are excluded from a filtered listing.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be 1..{MAX_PAGE_SIZE}")
    since = _iso_day(published_since, "published_since")
    until = _iso_day(published_until, "published_until")
    if since and until and since > until:
        raise ValueError("published_since must not be after published_until")
    if basis not in ("any", "dated", "new"):
        raise ValueError("basis must be any, dated or new")
    snapshot_id = snapshot_fingerprint(conn)
    after_id = decode_cursor(cursor, snapshot_id) if cursor else 0
    date_sql, date_params = "", []
    newly_column = _has_column(conn, "documents", "newly_found_at")
    if since or until:
        # "dated": source/header/metadata date in range; "new": first seen
        # after the source's baseline crawl within range; "any": either.
        columns = []
        if basis in ("any", "dated"):
            columns.append("substr(d.published_date, 1, 10)")
        if basis in ("any", "new") and newly_column:
            columns.append("substr(d.newly_found_at, 1, 10)")
        parts = []
        for column in columns:
            bounds = [f"{column} IS NOT NULL AND {column} != ''"]
            if since:
                bounds.append(f"{column} >= ?")
                date_params.append(since)
            if until:
                bounds.append(f"{column} <= ?")
                date_params.append(until)
            parts.append("(" + " AND ".join(bounds) + ")")
        date_sql = " AND (" + (" OR ".join(parts) if parts else "0") + ")"
    rows = conn.execute(
        """SELECT d.id, d.url, d.title, d.description, d.filename, d.file_type, d.mime_type,"""
        + (" d.published_date_source, d.newly_found_at," if newly_column else "")
        + """
                  d.published_date, d.discovered_at, d.downloaded_at, d.download_status,
                  d.sha256, d.file_size,
                  a.slug AS agency_slug, a.name AS agency_name,
                  o.slug AS office_slug, o.name AS office_name,
                  t.extraction_status, t.extraction_method, t.character_count, t.truncated
           FROM documents d
           LEFT JOIN agencies a ON a.id = d.agency_id
           LEFT JOIN offices o ON o.id = d.office_id
           LEFT JOIN document_text t ON t.document_id = d.id
           WHERE d.id > ? AND d.url IS NOT NULL"""
        + date_sql
        + """
           ORDER BY d.id LIMIT ?""",
        (after_id, *date_params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    sources_by_doc: Dict[int, List[Dict[str, Any]]] = {row["id"]: [] for row in rows}
    if rows:
        placeholders = ",".join("?" * len(rows))
        for source in conn.execute(
            f"""SELECT DISTINCT document_id, url FROM (
                    SELECT ds.document_id, rr.url FROM document_sources ds
                    JOIN reading_rooms rr ON rr.id = ds.reading_room_id
                    WHERE ds.document_id IN ({placeholders})
                    UNION
                    SELECT d.id, rr.url FROM documents d
                    JOIN reading_rooms rr ON rr.id = d.reading_room_id
                    WHERE d.id IN ({placeholders}))
                ORDER BY document_id, url""",
            [row["id"] for row in rows] * 2,
        ):
            sources_by_doc[source["document_id"]].append({
                "source_id": source_id_for(source["url"]),
                "url": normalize_url(source["url"]),
                "provenance": "official",
            })
    page = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "records": [_record(row, sources_by_doc[row["id"]]) for row in rows],
        "next_cursor": encode_cursor(rows[-1]["id"], snapshot_id) if has_more else None,
    }
    if since or until:
        # Additive envelope field; records keep the pinned v1 shape. Pass the
        # same filter with next_cursor to continue a filtered listing.
        page["filter"] = {
            "published_since": since,
            "published_until": until,
            "basis": basis,
            "note": "Records match when their release date (source page, server Last-Modified "
                    "or document metadata) or their first-seen-after-baseline time falls in the "
                    "range; records with neither are excluded. Coverage depends on which sources "
                    "have been crawled.",
            # Per-record match explanation (records keep the pinned v1 shape).
            "date_basis": {
                _record_id(row): _match_basis(row, since, until) for row in rows
            },
        }
    return page


# Additive manifest revisions keep schema_version "foia-catalog-export/1":
# 1.1 adds "producer" (name, repository, commit, commit_source).
CONTRACT_REVISION = "1.1"


def export_manifest(conn: sqlite3.Connection, *, now: Optional[datetime] = None, env=None) -> Dict[str, Any]:
    counts = conn.execute(
        """SELECT COUNT(*) AS records,
                  COALESCE(SUM(download_status='downloaded'),0) AS archived,
                  COUNT(DISTINCT agency_id) AS agencies
           FROM documents WHERE url IS NOT NULL"""
    ).fetchone()
    indexed = conn.execute(
        """SELECT COUNT(*) FROM document_text
           WHERE extraction_status IN ('indexed','indexed_truncated')"""
    ).fetchone()[0]
    sources = conn.execute("SELECT COUNT(*) FROM reading_rooms").fetchone()[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "producer": producer_info(env) if env is not None else producer_info(),
        "schema": "/api/v1/export/schema",
        "snapshot_id": snapshot_fingerprint(conn),
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "counts": {
            "records": counts["records"],
            "archived": counts["archived"],
            "metadata_only": counts["records"] - counts["archived"],
            "text_indexed": indexed,
            "agencies": counts["agencies"],
            "sources": sources,
        },
        "pagination": {"max_page_size": MAX_PAGE_SIZE, "default_page_size": DEFAULT_PAGE_SIZE,
                       "order": "stable within a snapshot; cursors expire when the snapshot changes"},
        "excluded_fields": ["storage_key", "local_path", "storage_backend", "download_error",
                            "requester_name"],
        "license": LICENSE_NOTE,
    }


def load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def write_snapshot(conn: sqlite3.Connection, output_dir: Path, *, page_size: int = MAX_PAGE_SIZE,
                   now: Optional[datetime] = None, env=None) -> Dict[str, Any]:
    """Write records-NNNNN.jsonl files plus a manifest with per-file SHA-256."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = export_manifest(conn, now=now, env=env)
    files: List[Dict[str, Any]] = []
    cursor, total = None, 0
    while True:
        page = export_page(conn, cursor=cursor, limit=page_size)
        if page["snapshot_id"] != manifest["snapshot_id"]:
            raise ExportCursorError("Archive changed during export; rerun against a stable snapshot")
        if not page["records"]:
            break
        name = f"records-{len(files) + 1:05d}.jsonl"
        body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in page["records"])
        (output_dir / name).write_text(body, encoding="utf-8")
        files.append({"path": name, "records": len(page["records"]),
                      "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
        total += len(page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    if total != manifest["counts"]["records"]:
        raise ExportCursorError("Exported record count does not match the manifest")
    manifest["files"] = files
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def verify_snapshot(output_dir: Path) -> Tuple[bool, List[str]]:
    """Replay check for a downloaded snapshot: hashes and counts must match."""
    output_dir = Path(output_dir)
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    problems, total, seen = [], 0, set()
    for entry in manifest.get("files", []):
        path = output_dir / entry["path"]
        if path.resolve().parent != output_dir.resolve():
            problems.append(f"{entry['path']}: path escapes snapshot directory")
            continue
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            problems.append(f"{entry['path']}: sha256 mismatch")
        try:
            lines = [json.loads(line) for line in body.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, ValueError):
            problems.append(f"{entry['path']}: not valid JSON lines")
            continue
        if len(lines) != entry["records"]:
            problems.append(f"{entry['path']}: record count mismatch")
        for record in lines:
            record_id = record.get("record_id") if isinstance(record, dict) else None
            if not isinstance(record_id, str):
                problems.append(f"{entry['path']}: record without record_id")
            elif record_id in seen:
                problems.append(f"duplicate record_id {record_id}")
            seen.add(record_id)
        total += len(lines)
    if total != manifest["counts"]["records"]:
        problems.append("total record count does not match manifest counts")
    return not problems, problems

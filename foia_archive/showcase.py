"""Showcase archive: a diverse, budget-capped set of original files.

The free storage tier (10 GB) cannot hold every original, so the archive keeps a
deliberately *diverse* selection: as many agencies as possible, a few documents
each, chosen round-robin with civil-liberties-relevant agencies first. Within
an agency, documents tied to a logged closed request come first, then documents
already stored (no re-download), then recent ones. Metadata, extracted text and
SHA-256 for every catalog record stay searchable regardless.

Applying a plan (owner-triggered only, typed confirmation):
1. originals in storage that are not in the selection are permanently deleted
   (every B2 version) and their records become ``text_only`` (or ``pending``
   when no text was extracted) - catalog rows and text are kept;
2. selected documents without a stored original are downloaded, text-extracted
   and stored, stopping at the byte budget.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CONFIRM_PHRASE = "REPLACE ARCHIVE WITH SHOWCASE"
DEFAULT_BUDGET_BYTES = 8_500_000_000  # AGENTS.md cap for archived originals
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_UNKNOWN_SIZE = 500 * 1024
SKIP_STATUSES = ("access_blocked", "http_error", "too_large", "content_mismatch")
FILE_TYPES = ("pdf", "docx", "doc", "xlsx", "xls", "csv", "txt", "rtf", "pptx")


def load_priorities(path: str | Path) -> List[Dict[str, Any]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    rules = []
    for tier in (1, 2):
        for item in data.get(f"tier{tier}") or []:
            rules.append({"tier": tier, "pattern": re.compile(str(item["match"]), re.IGNORECASE),
                          "why": str(item.get("why") or "")})
    return rules


def agency_tier(slug: str, name: str, rules) -> int:
    text = f"{slug} {name}"
    for rule in rules:
        if rule["pattern"].search(text):
            return rule["tier"]
    return 3


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS showcase_selection (
            document_id INTEGER PRIMARY KEY,
            agency_slug TEXT NOT NULL,
            tier INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            estimated_bytes INTEGER NOT NULL,
            reason TEXT NOT NULL,
            planned_at TEXT NOT NULL
        )"""
    )


def plan_showcase(conn: sqlite3.Connection, *, rules, budget_bytes: int = DEFAULT_BUDGET_BYTES,
                  max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                  per_agency_max: Optional[int] = None) -> Dict[str, Any]:
    """Choose documents round-robin across agencies within ``budget_bytes``."""
    has_matches = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='closed_request_matches'").fetchone())
    matched_sql = ("EXISTS(SELECT 1 FROM closed_request_matches m WHERE m.match_source='reading_room' "
                   "AND m.match_ref = CAST(d.id AS TEXT))") if has_matches else "0"
    placeholders = ",".join("?" * len(SKIP_STATUSES))
    type_placeholders = ",".join("?" * len(FILE_TYPES))
    rows = conn.execute(
        f"""SELECT d.id, d.url, d.file_type, d.file_size, d.remote_content_length, d.published_date,
                   d.storage_backend, d.storage_key, d.download_status, a.slug, a.name,
                   {matched_sql} AS log_matched
            FROM documents d JOIN agencies a ON a.id = d.agency_id
            WHERE COALESCE(d.download_status,'pending') NOT IN ({placeholders})
              AND LOWER(COALESCE(d.file_type,'')) IN ({type_placeholders})""",
        (*SKIP_STATUSES, *FILE_TYPES),
    ).fetchall()
    by_agency: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        size = row["file_size"] or row["remote_content_length"] or DEFAULT_UNKNOWN_SIZE
        if size > max_file_bytes:
            continue
        stored = bool(row["storage_key"]) and row["storage_backend"] not in (None, "none", "local")
        entry = by_agency.setdefault(row["slug"], {"name": row["name"], "tier": agency_tier(row["slug"], row["name"], rules),
                                                   "docs": []})
        entry["docs"].append({"id": row["id"], "size": int(size), "stored": stored,
                              "log_matched": bool(row["log_matched"]), "date": row["published_date"] or ""})
    for entry in by_agency.values():
        entry["docs"].sort(key=lambda d: (not d["log_matched"], not d["stored"], _neg_date(d["date"]), d["size"]))
    order = sorted(by_agency, key=lambda slug: (by_agency[slug]["tier"], slug))
    selected: List[Dict[str, Any]] = []
    used = 0
    position = {slug: 0 for slug in order}
    rank = 0
    progress = True
    while progress:
        progress = False
        for slug in order:
            docs = by_agency[slug]["docs"]
            taken = position[slug]
            if per_agency_max is not None and taken >= per_agency_max:
                continue
            while position[slug] < len(docs):
                doc = docs[position[slug]]
                position[slug] += 1
                if used + doc["size"] > budget_bytes:
                    continue
                used += doc["size"]
                rank += 1
                reason = ("log_matched" if doc["log_matched"] else "already_stored" if doc["stored"]
                          else "diversity")
                selected.append({"document_id": doc["id"], "agency_slug": slug,
                                 "tier": by_agency[slug]["tier"], "rank": rank,
                                 "estimated_bytes": doc["size"], "reason": reason})
                progress = True
                break
    stamp = datetime.now(timezone.utc).isoformat()
    create_tables(conn)
    with conn:
        conn.execute("DELETE FROM showcase_selection")
        conn.executemany(
            """INSERT INTO showcase_selection(document_id,agency_slug,tier,rank,estimated_bytes,reason,planned_at)
               VALUES(?,?,?,?,?,?,?)""",
            [(s["document_id"], s["agency_slug"], s["tier"], s["rank"], s["estimated_bytes"], s["reason"], stamp)
             for s in selected],
        )
    agencies = {s["agency_slug"] for s in selected}
    return {
        "budget_bytes": budget_bytes, "estimated_bytes": used, "documents": len(selected),
        "agencies_with_documents": len(by_agency), "agencies_selected": len(agencies),
        "by_tier": {t: len({s["agency_slug"] for s in selected if s["tier"] == t}) for t in (1, 2, 3)},
        "log_matched": sum(1 for s in selected if s["reason"] == "log_matched"),
        "already_stored": sum(1 for s in selected if s["reason"] == "already_stored"),
        "to_download": sum(1 for s in selected if s["reason"] != "already_stored"),
    }


def _neg_date(value: str) -> str:
    # Newest first in an ascending sort.
    return "".join(chr(255 - ord(c)) for c in value) if value else "\xff"


def prune_to_selection(conn: sqlite3.Connection, storage, delete_object, *, confirm: str,
                       prefix: str = "documents/") -> Dict[str, int]:
    """Delete stored originals that are not selected. Catalog rows and text stay."""
    if confirm != CONFIRM_PHRASE:
        raise PermissionError(f"type exactly: {CONFIRM_PHRASE}")
    create_tables(conn)
    keep = {row[0] for row in conn.execute(
        """SELECT d.storage_key FROM documents d JOIN showcase_selection s ON s.document_id = d.id
           WHERE d.storage_key IS NOT NULL""")}
    if not conn.execute("SELECT COUNT(*) FROM showcase_selection").fetchone()[0]:
        raise ValueError("no showcase plan: run plan-showcase first")
    manifest = storage.current_object_manifest(prefix=prefix)
    deleted = freed = 0
    for key, size in manifest.items():
        if key in keep:
            continue
        delete_object(storage, key)
        deleted += 1
        freed += int(size or 0)
        with conn:
            conn.execute(
                """UPDATE documents SET storage_backend='none', storage_key=NULL, local_path=NULL,
                       download_status = CASE WHEN EXISTS(SELECT 1 FROM document_text t
                           WHERE t.document_id = documents.id AND COALESCE(t.character_count,0) > 0)
                           THEN 'text_only' ELSE 'pending' END,
                       download_error = 'original removed: not in showcase selection'
                   WHERE storage_key = ?""",
                (key,),
            )
    return {"objects_listed": len(manifest), "deleted": deleted, "bytes_freed": freed, "kept": len(manifest) - deleted}


def fill_selection(conn: sqlite3.Connection, config, storage, *, budget_bytes: int,
                   download, rate_limiter, files_dir: Path) -> Dict[str, Any]:
    """Download, text-extract and store selected documents lacking an original."""
    from .scraper_core import DOCUMENT_MIME_TYPES, _filename_hint
    from .storage import archived_remote_bytes, update_download_failure, update_download_metadata, upsert_document_text
    from .text_extraction import OCRSettings, extract_document_text

    rows = conn.execute(
        """SELECT d.* FROM showcase_selection s JOIN documents d ON d.id = s.document_id
           WHERE d.storage_key IS NULL OR d.storage_backend IN ('none','local') ORDER BY s.rank"""
    ).fetchall()
    stored = failed = skipped = 0
    for row in rows:
        used = archived_remote_bytes(conn, storage.name)
        expected = row["file_size"] or row["remote_content_length"] or DEFAULT_UNKNOWN_SIZE
        if used + expected > budget_bytes:
            skipped += 1
            continue
        hint = _filename_hint(row["url"], row["title"] or "", row["file_type"] or "")
        stamp = datetime.now(timezone.utc).isoformat()
        result = download(row["url"], hint, config, rate_limiter=rate_limiter)
        if getattr(result, "status", None) != "downloaded" or result.path is None:
            with conn:
                update_download_failure(conn, row["id"], getattr(result, "status", "retryable_error") or "retryable_error",
                                        getattr(result, "error", None) or "showcase download failed", stamp)
            failed += 1
            continue
        try:
            if used + int(result.file_size or 0) > budget_bytes:
                skipped += 1
                continue
            kind = DOCUMENT_MIME_TYPES.get(result.mime_type or "", row["file_type"])
            extraction = extract_document_text(result.path, kind,
                                               ocr=OCRSettings.from_mapping(config.data.get("ocr")))
            location = storage.store_file(result.path, sha256=result.sha256, filename=hint,
                                          mime_type=result.mime_type, current_usage_bytes=used)
            with conn:
                update_download_metadata(conn, row["id"], location.local_path, stamp, mime_type=result.mime_type,
                                         file_size=result.file_size, sha256=result.sha256,
                                         storage_backend=location.backend, storage_key=location.key)
                upsert_document_text(conn, row["id"], body=extraction.text,
                                     extraction_status=extraction.status, extracted_at=stamp,
                                     extraction_error=extraction.error, extraction_method=extraction.method,
                                     character_count=extraction.character_count, truncated=extraction.truncated)
            stored += 1
        except Exception as exc:
            with conn:
                update_download_failure(conn, row["id"], "storage_error",
                                        f"showcase store failed: {type(exc).__name__}: {exc}"[:500], stamp)
            failed += 1
        finally:
            try:
                Path(result.path).unlink()
            except OSError:
                pass
    return {"candidates": len(rows), "stored": stored, "failed": failed, "skipped_budget": skipped,
            "archive_bytes": archived_remote_bytes(conn, storage.name)}


def showcase_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    create_tables(conn)
    return [dict(r) for r in conn.execute(
        """SELECT s.agency_slug, s.tier, COUNT(*) AS documents, SUM(s.estimated_bytes) AS estimated_bytes,
                  SUM(CASE WHEN d.storage_key IS NOT NULL AND d.storage_backend NOT IN ('none','local')
                      THEN 1 ELSE 0 END) AS stored
           FROM showcase_selection s JOIN documents d ON d.id = s.document_id
           GROUP BY s.agency_slug, s.tier ORDER BY s.tier, s.agency_slug""")]

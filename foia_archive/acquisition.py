"""FC-09: metadata-first growth past the binary cap.

When preserved-byte headroom is exhausted, discovery keeps cataloging record
metadata and source pointers; original bytes are queued for later acquisition
with priority, size hints and remote availability. Queued records stay
``metadata_only`` everywhere (FC-13 exports, coverage) — they are never
described as preserved or text-searchable.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

AVAILABILITY = ("unknown", "available", "broken", "blocked")


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS acquisition_queue (
        document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
        priority INTEGER NOT NULL,
        size_hint INTEGER,
        availability TEXT NOT NULL DEFAULT 'unknown'
            CHECK(availability IN ('unknown','available','broken','blocked')),
        availability_checked_at TEXT,
        reason TEXT NOT NULL,
        enqueued_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")


def growth_mode(archive_bytes: int, cap_bytes: int) -> str:
    """'binary' while under the cap, else 'metadata_only' (discovery continues)."""
    if type(archive_bytes) is not int or type(cap_bytes) is not int or archive_bytes < 0 or cap_bytes <= 0:
        raise ValueError("Byte counts must be non-negative integers and the cap positive")
    return "binary" if archive_bytes < cap_bytes else "metadata_only"


def enqueue_remote_only(conn: sqlite3.Connection, *, reason: str = "binary_cap_reached") -> int:
    """Queue every not-yet-preserved record. Agencies with nothing preserved go first."""
    create_tables(conn)
    stamp = datetime.now(timezone.utc).isoformat()
    with conn:
        return conn.execute(
            """INSERT INTO acquisition_queue(document_id,priority,size_hint,reason,enqueued_at,updated_at)
               SELECT d.id,
                      CASE WHEN NOT EXISTS (SELECT 1 FROM documents p WHERE p.agency_id IS d.agency_id
                                            AND p.download_status='downloaded') THEN 0 ELSE 1 END,
                      d.file_size, ?, ?, ?
               FROM documents d
               WHERE COALESCE(d.download_status,'pending') NOT IN ('downloaded')
                 AND d.url IS NOT NULL
               ON CONFLICT(document_id) DO NOTHING""",
            (reason, stamp, stamp)).rowcount


def record_availability(conn: sqlite3.Connection, document_id: int, availability: str) -> None:
    """Store remote-link evidence gathered by a permitted, rate-limited check."""
    if availability not in AVAILABILITY:
        raise ValueError("Unknown availability")
    stamp = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("UPDATE acquisition_queue SET availability=?, availability_checked_at=?, updated_at=? "
                     "WHERE document_id=?", (availability, stamp, stamp, document_id))


def next_acquisitions(conn: sqlite3.Connection, *, budget_bytes: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Plan the next fetches within remaining byte headroom.

    Known sizes must fit the remaining budget. An unknown-size record is only
    planned alone (nothing else committed), so a guessed size never overruns
    the cap; the downloader's own byte limits still apply. Broken/blocked
    links wait for new availability evidence.
    """
    if type(budget_bytes) is not int or budget_bytes < 0:
        raise ValueError("budget_bytes must be a non-negative integer")
    create_tables(conn)
    plan, used = [], 0
    for row in conn.execute(
        """SELECT q.document_id, q.priority, q.size_hint, q.availability, d.url
           FROM acquisition_queue q JOIN documents d ON d.id=q.document_id
           WHERE q.availability IN ('unknown','available') AND COALESCE(d.download_status,'') != 'downloaded'
           ORDER BY q.priority, (q.size_hint IS NULL), q.size_hint, q.document_id"""
    ):
        if len(plan) >= limit:
            break
        size = row["size_hint"]
        if size is None:
            if used:
                continue
        elif used + size > budget_bytes:
            continue
        plan.append(dict(row))
        used += size or 0
        if size is None:
            break  # one unknown-size fetch at a time; its real size is measured on download
    return plan


def queue_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    create_tables(conn)
    rows = conn.execute("SELECT availability, COUNT(*), COALESCE(SUM(size_hint),0), "
                        "COALESCE(SUM(size_hint IS NULL),0) FROM acquisition_queue GROUP BY 1").fetchall()
    return {r[0]: {"records": r[1], "known_bytes": r[2], "unknown_size": r[3]} for r in rows}

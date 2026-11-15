"""FC-05: historical and deep-archive backfill planning and evidence.

Backfill is planned as date windows on the FC-02 cursor ledger, so every
window is a replayable checkpoint: ``PENDING``/``IN_PROGRESS``/``FAILED``
windows form the missed-period queue (oldest first), ``EXHAUSTED`` windows
are verified traversals. Release-window evidence per source (oldest/newest
published record, per-year counts, empty years inside the observed span)
comes from the catalog. Years are metadata only; no political inference.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List

from .enumeration import create_tables as create_cursor_tables, date_windows


def plan_backfill(conn: sqlite3.Connection, source_id: str, start: date, end: date, *,
                  months: int = 12, strategy: str = "date_window") -> int:
    """Create PENDING windows (idempotent). Returns the number of new windows."""
    create_cursor_tables(conn)
    stamp = datetime.now(timezone.utc).isoformat()
    created = 0
    with conn:
        for window in date_windows(start, end, months):
            created += conn.execute(
                """INSERT INTO source_cursors(source_id,window_key,strategy,state,started_at,updated_at)
                   VALUES(?,?,?,'PENDING',?,?) ON CONFLICT(source_id,window_key) DO NOTHING""",
                (source_id, window["key"], strategy, stamp, stamp),
            ).rowcount
    return created


def missed_period_queue(conn: sqlite3.Connection, *, limit: int = 100) -> List[Dict[str, Any]]:
    """Unfinished windows across sources, oldest period first (BLOCKED excluded)."""
    create_cursor_tables(conn)
    rows = conn.execute(
        """SELECT source_id, window_key, state, pages_fetched, reason FROM source_cursors
           WHERE state IN ('PENDING','IN_PROGRESS','FAILED') AND window_key LIKE '____-__-__..____-__-__'
           ORDER BY window_key, source_id LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def backfill_status(conn: sqlite3.Connection, source_id: str) -> Dict[str, Any]:
    create_cursor_tables(conn)
    rows = conn.execute(
        """SELECT window_key, state, total_hint, items_seen FROM source_cursors
           WHERE source_id=? AND window_key LIKE '____-__-__..____-__-__' ORDER BY window_key""",
        (source_id,)).fetchall()
    states = defaultdict(int)
    for row in rows:
        states[row["state"]] += 1
    done = [r["window_key"] for r in rows if r["state"] == "EXHAUSTED"]
    return {
        "source_id": source_id, "windows": len(rows), "states": dict(states),
        "verified_oldest": done[0].split("..")[0] if done else None,
        "verified_newest": done[-1].split("..")[1] if done else None,
        "expected_vs_seen": [{"window": r["window_key"], "expected": r["total_hint"], "seen": r["items_seen"]}
                             for r in rows if r["total_hint"] is not None],
        "complete": bool(rows) and states.get("EXHAUSTED", 0) == len(rows),
    }


def release_windows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Per source: oldest/newest published record and empty years inside that span."""
    per_source: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    undated: Dict[str, int] = defaultdict(int)
    for row in conn.execute(
        """SELECT rr.url, d.published_date FROM document_sources ds
           JOIN reading_rooms rr ON rr.id = ds.reading_room_id JOIN documents d ON d.id = ds.document_id"""
    ):
        year = (row["published_date"] or "")[:4]
        if year.isdigit() and 1900 <= int(year) <= 2100:
            per_source[row["url"]][int(year)] += 1
        else:
            undated[row["url"]] += 1
    out = []
    for url in sorted(set(per_source) | set(undated)):
        years = per_source.get(url, {})
        span = (min(years), max(years)) if years else None
        out.append({
            "source_url": url, "oldest_year": span[0] if span else None, "newest_year": span[1] if span else None,
            "records_by_year": {str(y): years[y] for y in sorted(years)},
            "empty_years_in_span": [y for y in range(span[0], span[1] + 1) if y not in years] if span else [],
            "undated_records": undated.get(url, 0),
        })
    return out

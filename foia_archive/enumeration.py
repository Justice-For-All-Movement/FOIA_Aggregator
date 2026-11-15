"""FC-02: persistent source enumeration cursors and full-pagination traversal.

Every source is traversed page by page (or date window by date window) with
its cursor persisted after each page, so a crash or a page budget resumes at
the next unseen page instead of re-reading only the top page. Items are
de-duplicated per source across pages and windows. Pagination cycles,
unbounded/foreign next links and persistent access denials are recorded, not
retried or evaded.

Adapters supply ``fetch_page(cursor) -> Page``; this module never performs
network I/O itself, which keeps it testable against recorded fixtures.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from .scraper_core import SourceAccessBlocked

STATES = ("PENDING", "IN_PROGRESS", "EXHAUSTED", "BLOCKED", "FAILED")
MAX_PAGES_CEILING = 100_000


class PaginationError(RuntimeError):
    """The source's pagination is inconsistent; traversal stops for review."""


@dataclass(frozen=True)
class Page:
    """One fetched listing page.

    ``items`` are mappings with a stable ``key`` (usually the canonical
    document/request URL). ``next_cursor`` is ``None`` on the last page.
    ``total_hint`` is the source-declared result count, when published.
    """
    items: List[Dict[str, Any]]
    next_cursor: Optional[Dict[str, Any]]
    total_hint: Optional[int] = None


@dataclass
class TraversalResult:
    source_id: str
    window_key: str
    state: str
    pages_fetched: int
    items_seen: int
    new_items: int
    total_hint: Optional[int]
    reason: Optional[str] = None
    new_item_keys: List[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(cursor: Optional[Dict[str, Any]]) -> Optional[str]:
    if cursor is None:
        return None
    return json.dumps(cursor, sort_keys=True, separators=(",", ":"))


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_cursors (
            source_id TEXT NOT NULL,
            window_key TEXT NOT NULL,
            strategy TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('PENDING','IN_PROGRESS','EXHAUSTED','BLOCKED','FAILED')),
            cursor_json TEXT,
            seen_cursors_json TEXT NOT NULL DEFAULT '[]',
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            items_seen INTEGER NOT NULL DEFAULT 0,
            new_items INTEGER NOT NULL DEFAULT 0,
            total_hint INTEGER,
            reason TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (source_id, window_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_enumeration_items (
            source_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            first_window_key TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_id, item_key)
        )
        """
    )


def require_same_host(source_url: str, next_url: str) -> str:
    """Validate a 'next' link: HTTPS/HTTP on the source's own host only."""
    source, nxt = urlsplit(source_url), urlsplit(next_url)
    if nxt.scheme not in ("http", "https") or not nxt.hostname:
        raise PaginationError("Next link is not an absolute web URL")
    if nxt.username or nxt.password:
        raise PaginationError("Next link carries credentials")
    if (nxt.hostname or "").lower() != (source.hostname or "").lower():
        raise PaginationError("Next link leaves the source host")
    if source.scheme == "https" and nxt.scheme != "https":
        raise PaginationError("Next link downgrades to plain HTTP")
    return next_url


def date_windows(start: date, end: date, months: int) -> List[Dict[str, str]]:
    """Inclusive [start, end] split into consecutive month-sized windows."""
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 120:
        raise ValueError("Window size must be 1..120 months")
    if start > end:
        raise ValueError("Window start is after end")
    windows = []
    cursor = start
    while cursor <= end:
        month_index = cursor.month - 1 + months
        nxt = date(cursor.year + month_index // 12, month_index % 12 + 1, 1)
        last = min(end, date.fromordinal(nxt.toordinal() - 1))
        windows.append({"key": f"{cursor.isoformat()}..{last.isoformat()}",
                        "start": cursor.isoformat(), "end": last.isoformat()})
        cursor = nxt
    return windows


def _load(conn, source_id, window_key):
    return conn.execute(
        "SELECT * FROM source_cursors WHERE source_id=? AND window_key=?",
        (source_id, window_key),
    ).fetchone()


def enumerate_source(
    conn: sqlite3.Connection,
    source_id: str,
    fetch_page: Callable[[Dict[str, Any]], Page],
    *,
    initial_cursor: Dict[str, Any],
    strategy: str,
    window_key: str = "all",
    max_pages: int = 50,
    restart_exhausted: bool = False,
) -> TraversalResult:
    """Traverse (or resume) one source/window, persisting after each page.

    ``max_pages`` bounds one invocation; an unfinished traversal stays
    IN_PROGRESS and resumes from its stored cursor next time.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES_CEILING:
        raise ValueError("max_pages must be a bounded positive integer")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id required")
    create_tables(conn)
    row = _load(conn, source_id, window_key)
    stamp = _now()
    if row is None or (restart_exhausted and row["state"] == "EXHAUSTED"):
        conn.execute(
            """INSERT INTO source_cursors(source_id,window_key,strategy,state,cursor_json,
                   seen_cursors_json,started_at,updated_at)
               VALUES(?,?,?,'IN_PROGRESS',?,'[]',?,?)
               ON CONFLICT(source_id,window_key) DO UPDATE SET state='IN_PROGRESS',
                   cursor_json=excluded.cursor_json, seen_cursors_json='[]', pages_fetched=0,
                   items_seen=0, new_items=0, reason=NULL, completed_at=NULL,
                   started_at=excluded.started_at, updated_at=excluded.updated_at""",
            (source_id, window_key, strategy, _canonical(initial_cursor), stamp, stamp),
        )
        conn.commit()
        row = _load(conn, source_id, window_key)
    elif row["state"] in ("EXHAUSTED", "BLOCKED"):
        # Finished or access-denied traversals are not silently repeated.
        return TraversalResult(source_id, window_key, row["state"], 0, 0, 0,
                               row["total_hint"], row["reason"])
    elif row["state"] == "FAILED":
        conn.execute(
            "UPDATE source_cursors SET state='IN_PROGRESS', reason=NULL, updated_at=? "
            "WHERE source_id=? AND window_key=?", (stamp, source_id, window_key))
        conn.commit()

    cursor = json.loads(row["cursor_json"]) if row["cursor_json"] else dict(initial_cursor)
    seen = set(json.loads(row["seen_cursors_json"] or "[]"))
    result = TraversalResult(source_id, window_key, "IN_PROGRESS", 0, 0, 0, row["total_hint"])

    def finish(state, reason=None):
        result.state, result.reason = state, reason
        conn.execute(
            """UPDATE source_cursors SET state=?, reason=?, updated_at=?,
                   completed_at=CASE WHEN ? IN ('EXHAUSTED','BLOCKED') THEN ? ELSE completed_at END
               WHERE source_id=? AND window_key=?""",
            (state, reason, _now(), state, _now(), source_id, window_key),
        )
        conn.commit()
        return result

    for _ in range(max_pages):
        key = _canonical(cursor)
        if key in seen:
            return finish("FAILED", "pagination_cycle")
        try:
            page = fetch_page(dict(cursor))
        except SourceAccessBlocked as exc:
            return finish("BLOCKED", f"access_blocked: {str(exc)[:300]}")
        except PaginationError as exc:
            return finish("FAILED", f"pagination_error: {str(exc)[:300]}")
        except Exception as exc:  # cursor is preserved for the next attempt
            return finish("FAILED", type(exc).__name__)
        if not isinstance(page, Page) or not isinstance(page.items, list):
            return finish("FAILED", "invalid_page")
        item_keys = [item.get("key") if isinstance(item, dict) else None for item in page.items]
        if any(not isinstance(k, str) or not k for k in item_keys):
            return finish("FAILED", "item_without_stable_key")
        new_keys = []
        with conn:
            for item_key in item_keys:
                inserted = conn.execute(
                    """INSERT INTO source_enumeration_items(source_id,item_key,first_window_key,first_seen_at)
                       VALUES(?,?,?,?) ON CONFLICT(source_id,item_key) DO NOTHING""",
                    (source_id, item_key, window_key, _now()),
                ).rowcount
                if inserted:
                    new_keys.append(item_key)
            seen.add(key)
            hint = page.total_hint if isinstance(page.total_hint, int) and page.total_hint >= 0 else None
            conn.execute(
                """UPDATE source_cursors SET cursor_json=?, seen_cursors_json=?,
                       pages_fetched=pages_fetched+1, items_seen=items_seen+?,
                       new_items=new_items+?, total_hint=COALESCE(?, total_hint), updated_at=?
                   WHERE source_id=? AND window_key=?""",
                (_canonical(page.next_cursor), json.dumps(sorted(seen)), len(page.items),
                 len(new_keys), hint, _now(), source_id, window_key),
            )
        result.pages_fetched += 1
        result.items_seen += len(page.items)
        result.new_items += len(new_keys)
        result.new_item_keys.extend(new_keys)
        if hint is not None:
            result.total_hint = hint
        if page.next_cursor is None:
            return finish("EXHAUSTED")
        cursor = page.next_cursor
    # Page budget used: remain resumable.
    return finish("IN_PROGRESS", "page_budget_reached")


def enumerate_windows(
    conn: sqlite3.Connection,
    source_id: str,
    windows: Iterable[Dict[str, str]],
    fetch_page_for_window: Callable[[Dict[str, str], Dict[str, Any]], Page],
    *,
    initial_cursor: Dict[str, Any],
    strategy: str,
    max_pages_per_window: int = 50,
) -> List[TraversalResult]:
    """Traverse date-sliced windows (for sources that cap result depth)."""
    results = []
    for window in windows:
        results.append(enumerate_source(
            conn, source_id,
            lambda cursor, window=window: fetch_page_for_window(window, cursor),
            initial_cursor=initial_cursor, strategy=strategy,
            window_key=window["key"], max_pages=max_pages_per_window,
        ))
        if results[-1].state == "BLOCKED":
            break  # One denial applies to the whole source; do not hammer it.
    return results


def coverage_report(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Per-source enumeration evidence: windows, pages, unique items, hints."""
    create_tables(conn)
    rows = conn.execute(
        """SELECT c.source_id, COUNT(*) AS windows,
                  SUM(c.state='EXHAUSTED') AS windows_exhausted,
                  SUM(c.state='BLOCKED') AS windows_blocked,
                  SUM(c.state='FAILED') AS windows_failed,
                  SUM(c.state IN ('PENDING','IN_PROGRESS')) AS windows_open,
                  SUM(c.pages_fetched) AS pages_fetched,
                  SUM(c.total_hint) AS declared_total,
                  (SELECT COUNT(*) FROM source_enumeration_items i
                    WHERE i.source_id=c.source_id) AS unique_items
           FROM source_cursors c GROUP BY c.source_id ORDER BY c.source_id"""
    ).fetchall()
    report = []
    for row in rows:
        item = dict(row)
        item["fully_enumerated"] = item["windows"] == item["windows_exhausted"]
        declared = item["declared_total"]
        # A declared total that disagrees with what was traversed is evidence
        # of missed pages (or a stale hint) and must be reviewed, not assumed.
        item["declared_total_mismatch"] = (
            item["fully_enumerated"] and declared is not None
            and declared != item["unique_items"]
        )
        report.append(item)
    return report


def item_fingerprint(item: Dict[str, Any]) -> str:
    """Stable content fingerprint for a listing row (for change detection)."""
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

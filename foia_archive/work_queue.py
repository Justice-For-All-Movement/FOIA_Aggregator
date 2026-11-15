"""FC-10: durable leased work queue with per-host ceilings, fairness and backoff.

Items are idempotent per (kind, key). Workers lease batches atomically
(``BEGIN IMMEDIATE``); an expired lease (crashed worker) returns the item to
the pool. Leasing never exceeds a per-host concurrency ceiling, prefers
higher priority and agencies with the least completed work (untouched first),
and failures back off exponentially until ``DEAD``. SQLite remains the single
writer; horizontal scale beyond one host needs the FC-11 migration first.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

STATES = ("QUEUED", "LEASED", "DONE", "DEAD")


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS work_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, key TEXT NOT NULL, host TEXT NOT NULL, agency TEXT,
        priority INTEGER NOT NULL DEFAULT 100,
        state TEXT NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','LEASED','DONE','DEAD')),
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT, lease_expires REAL, not_before REAL NOT NULL DEFAULT 0,
        last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(kind, key))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_ready ON work_items(state, not_before, priority)")


def enqueue(conn: sqlite3.Connection, kind: str, key: str, *, host: str, agency: Optional[str] = None,
            priority: int = 100, now: float) -> bool:
    create_tables(conn)
    with conn:
        return conn.execute(
            """INSERT INTO work_items(kind,key,host,agency,priority,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,key) DO NOTHING""",
            (kind, key, host.lower(), agency, priority, now, now)).rowcount == 1


def lease(conn: sqlite3.Connection, worker: str, *, now: float, limit: int = 10,
          lease_seconds: float = 300, per_host_limit: int = 2) -> List[Dict[str, Any]]:
    if not 1 <= limit <= 1000 or not 1 <= per_host_limit <= 64 or lease_seconds <= 0:
        raise ValueError("Unbounded lease request")
    create_tables(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        active: Dict[str, int] = {r[0]: r[1] for r in conn.execute(
            "SELECT host, COUNT(*) FROM work_items WHERE state='LEASED' AND lease_expires > ? GROUP BY host", (now,))}
        # Round-robin across agencies (rank within agency), weighted by work
        # already completed, so a large agency never starves untouched ones.
        ranked = conn.execute(
            """WITH done AS (SELECT agency, COUNT(*) AS n FROM work_items WHERE state='DONE' GROUP BY agency),
                    ready AS (SELECT w.*, ROW_NUMBER() OVER (PARTITION BY w.agency ORDER BY w.priority, w.id) AS rn
                              FROM work_items w
                              WHERE ((w.state='QUEUED') OR (w.state='LEASED' AND w.lease_expires <= ?))
                                AND w.not_before <= ?)
               SELECT ready.* FROM ready LEFT JOIN done ON done.agency IS ready.agency
               ORDER BY ready.priority, COALESCE(done.n, 0) + ready.rn, ready.id
               LIMIT ?""", (now, now, limit * 50)).fetchall()
        chosen = []
        for row in ranked:
            if len(chosen) >= limit:
                break
            if active.get(row["host"], 0) >= per_host_limit:
                continue
            active[row["host"]] = active.get(row["host"], 0) + 1
            chosen.append(row)
        for row in chosen:
            conn.execute("UPDATE work_items SET state='LEASED', lease_owner=?, lease_expires=?, attempts=attempts+1, "
                         "updated_at=? WHERE id=?", (worker, now + lease_seconds, now, row["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return [dict(r) for r in chosen]


def _owned(conn, item_id, worker, now) -> bool:
    row = conn.execute("SELECT state, lease_owner, lease_expires FROM work_items WHERE id=?", (item_id,)).fetchone()
    return bool(row) and row["state"] == "LEASED" and row["lease_owner"] == worker and row["lease_expires"] > now


def heartbeat(conn, item_id: int, worker: str, *, now: float, lease_seconds: float = 300) -> bool:
    with conn:
        if not _owned(conn, item_id, worker, now):
            return False
        conn.execute("UPDATE work_items SET lease_expires=?, updated_at=? WHERE id=?", (now + lease_seconds, now, item_id))
        return True


def complete(conn, item_id: int, worker: str, *, now: float) -> bool:
    """Only the current lease holder may complete; a stale worker's result is refused."""
    with conn:
        if not _owned(conn, item_id, worker, now):
            return False
        conn.execute("UPDATE work_items SET state='DONE', lease_owner=NULL, lease_expires=NULL, updated_at=? "
                     "WHERE id=?", (now, item_id))
        return True


def fail(conn, item_id: int, worker: str, error: str, *, now: float, max_attempts: int = 5,
         base_delay: float = 60, max_delay: float = 6 * 3600) -> Optional[str]:
    with conn:
        if not _owned(conn, item_id, worker, now):
            return None
        attempts = conn.execute("SELECT attempts FROM work_items WHERE id=?", (item_id,)).fetchone()[0]
        state = "DEAD" if attempts >= max_attempts else "QUEUED"
        delay = min(max_delay, base_delay * 2 ** (attempts - 1))
        conn.execute("UPDATE work_items SET state=?, lease_owner=NULL, lease_expires=NULL, not_before=?, "
                     "last_error=?, updated_at=? WHERE id=?", (state, now + delay, error[:500], now, item_id))
        return state


def stats(conn) -> Dict[str, int]:
    create_tables(conn)
    out = dict.fromkeys(STATES, 0)
    for state, n in conn.execute("SELECT state, COUNT(*) FROM work_items GROUP BY state"):
        out[state] = n
    return out

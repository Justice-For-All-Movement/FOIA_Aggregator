"""Optional Internet Archive (Wayback Machine) preservation of archived records.

Off by default (``FOIA_WAYBACK_ENABLED=true`` enables it). For each record we
fetched (original kept or text-only), newest first:

1. Ask the public availability API whether a snapshot already exists; if so,
   record it and submit nothing.
2. Otherwise submit the public URL to Save Page Now: SPN2 with
   ``IA_S3_ACCESS_KEY``/``IA_S3_SECRET_KEY`` when set (job id polled briefly,
   pending jobs re-checked next run), else anonymous mode.

Politeness: a fixed delay between submissions (default 12 s), a per-run cap
(default 40), and any 429/limit response stops submissions for the run.
The snapshot URL is stored and shown on the record page.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote

import requests

from .utils import logger, parse_bool

AVAILABILITY_URL = "https://archive.org/wayback/available"
SPN_URL = "https://web.archive.org/save"
STATUS_URL = "https://web.archive.org/save/status/"
SNAPSHOT_RE = re.compile(r"^/web/(\d{14})/(.+)$")


def create_tables(conn: sqlite3.Connection) -> None:
    from .storage import _ensure_columns, _table_columns
    if "url" in _table_columns(conn, "documents"):
        _ensure_columns(conn, "documents", {
            "wayback_url": "TEXT",
            "wayback_status": "TEXT",
            "wayback_job_id": "TEXT",
            "wayback_checked_at": "TEXT",
        })
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_wayback_status ON documents(wayback_status)")


def enabled() -> bool:
    try:
        return bool(parse_bool(os.getenv("FOIA_WAYBACK_ENABLED") or "false"))
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_url(timestamp: str, original: str) -> str:
    return f"https://web.archive.org/web/{timestamp}/{original}"


class WaybackClient:
    def __init__(self, user_agent: str, *, access_key: str = "", secret_key: str = "",
                 session=None, sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if access_key and secret_key:
            self.headers["Authorization"] = f"LOW {access_key}:{secret_key}"
        self.authenticated = bool(access_key and secret_key)
        self.sleep = sleep

    def existing(self, url: str) -> Optional[str]:
        response = self.session.get(AVAILABILITY_URL, params={"url": url},
                                    headers={"User-Agent": self.headers["User-Agent"]}, timeout=30)
        try:
            if response.status_code == 429:
                raise RateLimited("availability API rate limited")
            if response.status_code != 200:
                return None
            closest = ((response.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
        finally:
            response.close()
        if closest.get("available") and str(closest.get("status", "200")).startswith("2") and closest.get("url"):
            return str(closest["url"]).replace("http://web.archive.org", "https://web.archive.org")
        return None

    def save(self, url: str) -> tuple[str, Optional[str], Optional[str]]:
        """Return (status, snapshot_url, job_id): saved | pending | failed."""
        if self.authenticated:
            response = self.session.post(SPN_URL, headers=self.headers, timeout=60,
                                         data={"url": url, "skip_first_archive": "1"})
            try:
                _raise_if_limited(response)
                if response.status_code != 200:
                    return "failed", None, None
                payload = response.json() or {}
            finally:
                response.close()
            job_id = payload.get("job_id")
            if not job_id:
                return "failed", None, None
            for _ in range(3):
                self.sleep(10)
                status, snapshot = self.status(job_id)
                if status != "pending":
                    return status, snapshot, job_id
            return "pending", None, job_id
        response = self.session.get(f"{SPN_URL}/{quote(url, safe=':/?=&%')}",
                                    headers={"User-Agent": self.headers["User-Agent"]},
                                    timeout=120, allow_redirects=False)
        try:
            _raise_if_limited(response)
            location = response.headers.get("Content-Location") or response.headers.get("Location") or ""
            ok = response.status_code in (200, 301, 302)
        finally:
            response.close()
        location = location.replace("https://web.archive.org", "")
        match = SNAPSHOT_RE.match(location)
        if match:
            return "saved", _snapshot_url(match.group(1), match.group(2)), None
        return ("pending" if ok else "failed"), None, None

    def status(self, job_id: str) -> tuple[str, Optional[str]]:
        response = self.session.get(STATUS_URL + quote(job_id, safe=""), headers=self.headers, timeout=30)
        try:
            _raise_if_limited(response)
            payload = response.json() if response.status_code == 200 else {}
        finally:
            response.close()
        state = (payload or {}).get("status")
        if state == "success" and payload.get("timestamp") and payload.get("original_url"):
            return "saved", _snapshot_url(payload["timestamp"], payload["original_url"])
        if state == "pending":
            return "pending", None
        return "failed", None


class RateLimited(RuntimeError):
    pass


def _raise_if_limited(response) -> None:
    if response.status_code == 429:
        raise RateLimited("Save Page Now rate limit (HTTP 429)")
    if response.status_code == 200 and "application/json" in (response.headers.get("Content-Type") or ""):
        try:
            payload = response.json()
        except ValueError:
            return
        if isinstance(payload, dict) and str(payload.get("status_ext", "")).startswith("error:too-many"):
            raise RateLimited(str(payload.get("status_ext")))


def preserve(conn: sqlite3.Connection, user_agent: str, *, max_per_run: Optional[int] = None,
             delay_seconds: Optional[float] = None, client: Optional[WaybackClient] = None,
             sleep: Callable[[float], None] = time.sleep) -> dict:
    """Record or request Wayback snapshots for fetched records (newest first)."""
    create_tables(conn)
    max_per_run = int(max_per_run if max_per_run is not None else os.getenv("FOIA_WAYBACK_MAX_PER_RUN", "40"))
    delay = float(delay_seconds if delay_seconds is not None else os.getenv("FOIA_WAYBACK_DELAY_SECONDS", "12"))
    delay = max(5.0, delay) if client is None else max(0.0, delay)
    client = client or WaybackClient(
        user_agent,
        access_key=(os.getenv("IA_S3_ACCESS_KEY") or "").strip(),
        secret_key=(os.getenv("IA_S3_SECRET_KEY") or "").strip(),
        sleep=sleep,
    )
    counts = {"existing": 0, "saved": 0, "pending": 0, "failed": 0, "rate_limited": False}
    pending = conn.execute(
        "SELECT id, wayback_job_id FROM documents WHERE wayback_status = 'pending' "
        "AND wayback_job_id IS NOT NULL LIMIT ?", (max_per_run,)).fetchall() if client.authenticated else []
    rows = conn.execute(
        """
        SELECT id, url FROM documents
        WHERE download_status IN ('downloaded', 'text_only')
          AND (wayback_status IS NULL OR (wayback_status = 'pending' AND wayback_job_id IS NULL))
        ORDER BY COALESCE(newly_found_at, published_date, discovered_at) DESC, id DESC
        LIMIT ?
        """,
        (max_per_run,),
    ).fetchall()
    try:
        for row in pending:
            status, snapshot = client.status(row["wayback_job_id"])
            _record(conn, row["id"], status, snapshot, row["wayback_job_id"])
            counts[status] += 1
        for index, row in enumerate(rows):
            snapshot = client.existing(row["url"])
            if snapshot:
                _record(conn, row["id"], "existing", snapshot, None)
                counts["existing"] += 1
                continue
            if index:
                sleep(delay)
            status, snapshot, job_id = client.save(row["url"])
            _record(conn, row["id"], status, snapshot, job_id)
            counts[status] += 1
    except RateLimited as exc:
        counts["rate_limited"] = True
        logger.info("Wayback submissions paused for this run: %s", exc)
    except requests.RequestException as exc:
        logger.info("Wayback preservation interrupted: %s", type(exc).__name__)
    return counts


def _record(conn, doc_id: int, status: str, snapshot: Optional[str], job_id: Optional[str]) -> None:
    conn.execute(
        "UPDATE documents SET wayback_status = ?, wayback_url = COALESCE(?, wayback_url), "
        "wayback_job_id = ?, wayback_checked_at = ? WHERE id = ?",
        (status, snapshot, job_id, _now(), doc_id),
    )
    conn.commit()

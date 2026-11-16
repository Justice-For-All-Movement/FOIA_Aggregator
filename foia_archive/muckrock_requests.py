"""FC-04 request-listing mode: public MuckRock FOIA requests within a date window.

Answers "which FOIA requests did others file (or update/complete) between
these dates?" from MuckRock's API v2 (``/api_v2/requests/``). The API shape
was taken from MuckRock's public source (see docs/FC04_MUCKROCK_ADAPTER.md):

* list endpoint requires an API token (``AuthenticatedAPIMixin``); the token
  is read from ``MUCKROCK_API_TOKEN`` and the feature is disabled without it;
* filters ``datetime_submitted__gte/__lte``, ``datetime_updated__gte/__lte``,
  ``datetime_done__gte/__lte``, ``status``, ``embargo_status``, ``agency``;
* cursor pagination ordered by primary key, ``page_size`` up to 100, results
  under ``results`` with a ``next`` link.

Stored per request: id, title, status, agency id/name, tracking id, tags,
submitted/updated/done dates and the public MuckRock URL when the API gives
one. Requester identity (``user``, collaborators), the request text and any
contact details are never stored. Only ``embargo_status == "public"`` rows
are kept. Persistent 401/403 (including Cloudflare challenges) are recorded
as BLOCKED and never evaded.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from .enumeration import Page, PaginationError, enumerate_source, require_same_host
from .source_inventory import source_id_for

API_V2_ROOT = "https://www.muckrock.com/api_v2/"
REQUESTS_URL = API_V2_ROOT + "requests/"
SITE_ROOT = "https://www.muckrock.com/"
DATE_FIELDS = {"submitted": "datetime_submitted", "updated": "datetime_updated", "done": "datetime_done"}
TOKEN_ENV = "MUCKROCK_API_TOKEN"
MAX_AGENCY_LOOKUPS = 50


class MuckRockDisabled(RuntimeError):
    """No MUCKROCK_API_TOKEN is configured; the listing mode stays off."""


def api_token(env=None) -> Optional[str]:
    value = ((env if env is not None else os.environ).get(TOKEN_ENV) or "").strip()
    return value or None


def build_list_url(since: Optional[str], until: Optional[str], *, date_field: str = "submitted",
                   page_size: int = 100, status: Optional[str] = None) -> str:
    if date_field not in DATE_FIELDS:
        raise ValueError(f"date_field must be one of {sorted(DATE_FIELDS)}")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be 1..100")
    field = DATE_FIELDS[date_field]
    params: Dict[str, Any] = {"page_size": page_size, "embargo_status": "public"}
    if since:
        params[f"{field}__gte"] = since
    if until:
        # Inclusive day: the filter is a datetime, so use the end of that day.
        params[f"{field}__lte"] = f"{until}T23:59:59"
    if status:
        params["status"] = status
    return REQUESTS_URL + "?" + urlencode(params)


def _public_url(item: Dict[str, Any]) -> Optional[str]:
    raw = item.get("absolute_url")
    if not isinstance(raw, str) or not raw.strip():
        return None
    url = urljoin(SITE_ROOT, raw.strip())
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or not (
            host == "muckrock.com" or host.endswith(".muckrock.com")):
        return None
    return url


def _text(value: Any, limit: int = 1000) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def parse_request(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one API v2 request; None for non-public (embargoed) rows."""
    if not isinstance(item, dict) or item.get("id") is None:
        raise ValueError("MuckRock request without an id")
    embargo = item.get("embargo_status")
    if embargo not in (None, "public"):
        return None
    agency = item.get("agency")
    agency_ref = agency.get("id") if isinstance(agency, dict) else agency
    agency_name = agency.get("name") if isinstance(agency, dict) else None
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    return {
        "request_id": str(item["id"]),
        "title": _text(item.get("title"), 500),
        "status": _text(item.get("status"), 64),
        "agency_ref": None if agency_ref is None else str(agency_ref),
        "agency_name": _text(agency_name, 300),
        "tracking_id": _text(item.get("tracking_id"), 200),
        "tags": ",".join(sorted({t for t in (_text(x, 80) for x in tags) if t}))[:1000] or None,
        "submitted_at": _text(item.get("datetime_submitted"), 40),
        "updated_at": _text(item.get("datetime_updated"), 40),
        "done_at": _text(item.get("datetime_done"), 40),
        "public_url": _public_url(item),
        # Deliberately absent: user, edit/read collaborators, requested_docs,
        # composer, price and any requester contact details.
    }


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS third_party_requests (
            publisher TEXT NOT NULL,
            request_id TEXT NOT NULL,
            title TEXT,
            status TEXT,
            agency_ref TEXT,
            agency_name TEXT,
            tracking_id TEXT,
            tags TEXT,
            submitted_at TEXT,
            updated_at TEXT,
            done_at TEXT,
            public_url TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (publisher, request_id)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_third_party_requests_submitted "
                 "ON third_party_requests(submitted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_third_party_requests_updated "
                 "ON third_party_requests(updated_at)")


def store_requests(conn: sqlite3.Connection, rows: List[Dict[str, Any]], publisher: str = "MuckRock") -> int:
    create_tables(conn)
    stamp = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with conn:
        for row in rows:
            exists = conn.execute("SELECT 1 FROM third_party_requests WHERE publisher=? AND request_id=?",
                                  (publisher, row["request_id"])).fetchone()
            conn.execute(
                """INSERT INTO third_party_requests(publisher,request_id,title,status,agency_ref,agency_name,
                       tracking_id,tags,submitted_at,updated_at,done_at,public_url,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(publisher,request_id) DO UPDATE SET title=excluded.title,
                       status=excluded.status, agency_ref=excluded.agency_ref,
                       agency_name=COALESCE(excluded.agency_name, third_party_requests.agency_name),
                       tracking_id=excluded.tracking_id, tags=excluded.tags,
                       submitted_at=excluded.submitted_at, updated_at=excluded.updated_at,
                       done_at=excluded.done_at,
                       public_url=COALESCE(excluded.public_url, third_party_requests.public_url),
                       last_seen_at=excluded.last_seen_at""",
                (publisher, row["request_id"], row["title"], row["status"], row["agency_ref"],
                 row["agency_name"], row["tracking_id"], row["tags"], row["submitted_at"],
                 row["updated_at"], row["done_at"], row["public_url"], stamp, stamp),
            )
            inserted += 0 if exists else 1
    return inserted


def parse_page(payload: Dict[str, Any], *, page_url: str) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise PaginationError("Unexpected MuckRock API v2 page shape")
    next_url = payload.get("next")
    next_cursor = {"url": require_same_host(page_url, next_url)} if next_url else None
    rows, embargoed = [], 0
    for item in payload["results"]:
        parsed = parse_request(item)
        if parsed is None:
            embargoed += 1
        else:
            rows.append(parsed)
    count = payload.get("count")
    return {"rows": rows, "embargoed": embargoed, "next_cursor": next_cursor,
            "count": count if isinstance(count, int) and count >= 0 else None}


def list_requests(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], Dict[str, Any]],
    *,
    since: Optional[str],
    until: Optional[str],
    date_field: str = "submitted",
    max_pages: int = 5,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Bounded, resumable listing of public requests within a date window.

    ``fetch_json`` must perform the authenticated, rate-limited request and
    raise ``SourceAccessBlocked`` on 401/403. Agency names are resolved for
    at most ``MAX_AGENCY_LOOKUPS`` agencies per run.
    """
    start_url = build_list_url(since, until, date_field=date_field, status=status)
    source_id = source_id_for(REQUESTS_URL)
    window_key = f"requests:{date_field}:{since or ''}:{until or ''}:{status or ''}"
    stats = {"requests_seen": 0, "requests_new": 0, "embargoed_skipped": 0, "agency_lookups": 0}
    agency_names: Dict[str, Optional[str]] = {}

    def agency_name(ref: Optional[str]) -> Optional[str]:
        if ref is None or not ref.isdigit():
            return None
        if ref not in agency_names:
            if stats["agency_lookups"] >= MAX_AGENCY_LOOKUPS:
                return None
            stats["agency_lookups"] += 1
            try:
                payload = fetch_json(f"{API_V2_ROOT}agencies/{ref}/")
                agency_names[ref] = _text(payload.get("name"), 300) if isinstance(payload, dict) else None
            except Exception as exc:  # noqa: BLE001 - a name is optional, blocks are not
                from .scraper_core import SourceAccessBlocked
                if isinstance(exc, SourceAccessBlocked):
                    raise
                agency_names[ref] = None
        return agency_names[ref]

    def fetch_page(cursor: Dict[str, Any]) -> Page:
        url = require_same_host(start_url, cursor["url"])
        page = parse_page(fetch_json(url), page_url=url)
        for row in page["rows"]:
            if row["agency_name"] is None:
                row["agency_name"] = agency_name(row["agency_ref"])
        stats["requests_seen"] += len(page["rows"])
        stats["embargoed_skipped"] += page["embargoed"]
        stats["requests_new"] += store_requests(conn, page["rows"])
        return Page([{"key": f"muckrock:request:{r['request_id']}"} for r in page["rows"]],
                    page["next_cursor"], page["count"])

    result = enumerate_source(conn, source_id, fetch_page, initial_cursor={"url": start_url},
                              strategy="json_api_next_link", window_key=window_key,
                              max_pages=max_pages, restart_exhausted=True)
    return {"source_id": source_id, "window_key": window_key, "state": result.state,
            "reason": result.reason, "pages_fetched": result.pages_fetched, **stats}


def query_requests(conn: sqlite3.Connection, *, since: Optional[str], until: Optional[str],
                   date_field: str = "submitted", limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Read path for the site/API; tolerant of snapshots without the table."""
    column = {"submitted": "submitted_at", "updated": "updated_at", "done": "done_at"}.get(date_field)
    if column is None:
        raise ValueError("date_field must be submitted, updated or done")
    present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='third_party_requests'"
                           ).fetchone() is not None
    if not present:
        return {"available": False, "total": 0, "requests": []}
    clauses, params = [f"{column} IS NOT NULL AND {column} != ''"], []
    if since:
        clauses.append(f"substr({column},1,10) >= ?")
        params.append(since)
    if until:
        clauses.append(f"substr({column},1,10) <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM third_party_requests WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT publisher, request_id, title, status, agency_ref, agency_name, tracking_id, tags,
                   submitted_at, updated_at, done_at, public_url
            FROM third_party_requests WHERE {where}
            ORDER BY {column} DESC, request_id DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return {"available": True, "total": total, "requests": [dict(r) for r in rows]}

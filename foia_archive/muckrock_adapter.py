"""FC-04: MuckRock third-party release adapter (recorded fixtures; inert by default).

MuckRock publishes public FOIA requests, agency communications and attached
files through a paginated JSON API (``/api_v1/foia/`` with a ``next`` link).
This adapter turns those pages into third-party release records with
separate attribution for:

* the original **agency** that released the records,
* the **publisher** (MuckRock) and the public request page,
* each released **file** (URL, title, date) — only files attached to agency
  *response* communications. A request page, a requester's own upload, or a
  request without responsive files is never treated as a released document.

The adapter performs no network I/O itself: callers inject ``fetch_json``
(which must honour robots, declared rate limits and terms, and raise
``SourceAccessBlocked`` on persistent denials). Nothing schedules it yet.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from .enumeration import Page, PaginationError, enumerate_source, require_same_host
from .source_inventory import normalize_url, source_id_for

PUBLISHER = "MuckRock"
API_ROOT = "https://www.muckrock.com/api_v1/foia/"
SITE_ROOT = "https://www.muckrock.com/"
ALLOWED_FILE_HOSTS = ("muckrock.com", "www.muckrock.com", "cdn.muckrock.com",
                      "muckrock.s3.amazonaws.com")


@dataclass(frozen=True)
class ThirdPartyRelease:
    release_id: str
    publisher: str
    request_id: str
    request_url: str
    request_title: Optional[str]
    request_status: Optional[str]
    agency_ref: Optional[str]
    agency_name: Optional[str]
    file_url: str
    file_title: Optional[str]
    released_at: Optional[str]
    communication_index: int


def _https_url(value: Any, base: str = SITE_ROOT) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    url = urljoin(base, value.strip())
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return url


def _file_host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "s3.amazonaws.com":  # path-style URL into MuckRock's own bucket only
        return parts.path.startswith("/muckrock/")
    return host in ALLOWED_FILE_HOSTS or host.endswith(".muckrock.com")


def parse_request(item: Dict[str, Any], agencies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Normalize one API request object into a request record + released files."""
    if not isinstance(item, dict) or item.get("id") is None:
        raise ValueError("MuckRock request without an id")
    request_id = str(item["id"])
    request_url = _https_url(item.get("absolute_url")) or urljoin(SITE_ROOT, f"foi/{item.get('slug') or ''}-{request_id}/")
    agency = item.get("agency")
    agency_ref = None if agency is None else str(agency.get("id") if isinstance(agency, dict) else agency)
    agency_name = agency.get("name") if isinstance(agency, dict) else (agencies or {}).get(agency_ref)
    releases: List[ThirdPartyRelease] = []
    skipped = {"non_response_files": 0, "unsafe_file_urls": 0}
    for index, comm in enumerate(item.get("communications") or []):
        if not isinstance(comm, dict):
            continue
        files = comm.get("files") or []
        if not comm.get("response"):
            skipped["non_response_files"] += len(files)
            continue
        for file in files:
            url = _https_url((file or {}).get("ffile") or (file or {}).get("url"))
            if not url or not _file_host_allowed(url):
                skipped["unsafe_file_urls"] += 1
                continue
            release_id = "tpr_" + hashlib.sha256(
                f"{PUBLISHER}\x00{request_id}\x00{normalize_url(url)}".encode()).hexdigest()[:24]
            releases.append(ThirdPartyRelease(
                release_id=release_id, publisher=PUBLISHER, request_id=request_id,
                request_url=request_url, request_title=item.get("title"),
                request_status=item.get("status"), agency_ref=agency_ref, agency_name=agency_name,
                file_url=url, file_title=file.get("title"),
                released_at=file.get("datetime") or comm.get("datetime"),
                communication_index=index,
            ))
    return {
        "request": {
            "request_id": request_id, "request_url": request_url, "title": item.get("title"),
            "status": item.get("status"), "agency_ref": agency_ref, "agency_name": agency_name,
            "tracking_id": item.get("tracking_id"), "submitted_at": item.get("datetime_submitted"),
            "completed_at": item.get("datetime_done"), "publisher": PUBLISHER,
        },
        "releases": releases,
        "skipped": skipped,
    }


def parse_page(payload: Dict[str, Any], *, page_url: str) -> Page:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise PaginationError("Unexpected MuckRock page shape")
    next_url = payload.get("next")
    next_cursor = {"url": require_same_host(page_url, next_url)} if next_url else None
    items = []
    for item in payload["results"]:
        parsed = parse_request(item)
        items.append({"key": parsed["request"]["request_url"], "parsed": parsed})
    count = payload.get("count")
    return Page(items, next_cursor, count if isinstance(count, int) and count >= 0 else None)


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS third_party_releases (
            release_id TEXT PRIMARY KEY,
            publisher TEXT NOT NULL,
            source_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_url TEXT NOT NULL,
            request_title TEXT,
            request_status TEXT,
            agency_ref TEXT,
            agency_name TEXT,
            file_url TEXT NOT NULL,
            file_title TEXT,
            released_at TEXT,
            communication_index INTEGER NOT NULL,
            official_document_id INTEGER REFERENCES documents(id),
            first_seen_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_third_party_releases_file ON third_party_releases(file_url)")


def store_releases(conn: sqlite3.Connection, releases: List[ThirdPartyRelease], source_id: str) -> int:
    """Insert new releases; crosslink to an official document only on an exact URL match."""
    create_tables(conn)
    stamp = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with conn:
        for release in releases:
            official = conn.execute("SELECT id FROM documents WHERE url=?", (release.file_url,)).fetchone()
            inserted += conn.execute(
                """INSERT INTO third_party_releases(release_id,publisher,source_id,request_id,request_url,
                       request_title,request_status,agency_ref,agency_name,file_url,file_title,
                       released_at,communication_index,official_document_id,first_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(release_id) DO NOTHING""",
                (release.release_id, release.publisher, source_id, release.request_id, release.request_url,
                 release.request_title, release.request_status, release.agency_ref, release.agency_name,
                 release.file_url, release.file_title, release.released_at, release.communication_index,
                 official[0] if official else None, stamp),
            ).rowcount
    return inserted


def ingest(
    conn: sqlite3.Connection,
    fetch_json: Callable[[str], Dict[str, Any]],
    *,
    start_url: str = API_ROOT + "?format=json&status=done",
    max_pages: int = 5,
) -> Dict[str, Any]:
    """Bounded, resumable traversal (FC-02 cursor) storing response files only."""
    source_id = source_id_for(API_ROOT)
    stats = {"requests": 0, "releases_new": 0, "non_response_files": 0, "unsafe_file_urls": 0}

    def fetch_page(cursor: Dict[str, Any]) -> Page:
        url = require_same_host(start_url, cursor["url"])
        page = parse_page(fetch_json(url), page_url=url)
        for item in page.items:
            parsed = item["parsed"]
            stats["requests"] += 1
            stats["releases_new"] += store_releases(conn, parsed["releases"], source_id)
            for key in ("non_response_files", "unsafe_file_urls"):
                stats[key] += parsed["skipped"][key]
        # Only the stable key goes to the enumeration ledger.
        return Page([{"key": i["key"]} for i in page.items], page.next_cursor, page.total_hint)

    result = enumerate_source(conn, source_id, fetch_page, initial_cursor={"url": start_url},
                              strategy="json_api_next_link", max_pages=max_pages)
    return {"source_id": source_id, "state": result.state, "reason": result.reason,
            "pages_fetched": result.pages_fetched, **stats}


def release_as_dict(release: ThirdPartyRelease) -> Dict[str, Any]:
    return json.loads(json.dumps(asdict(release)))

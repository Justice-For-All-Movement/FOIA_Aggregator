"""Scrape FOIA request logs that agencies already publish.

Each registry entry (``config/published_logs.yaml``) names an agency's official
log index page. The scraper fetches that page politely (honest UA, robots.txt,
per-host delay, SSRF-safe redirects), picks links to log files (CSV, TSV, XLSX,
XLS, PDF) whose URL or link text matches the entry's pattern, downloads at most
``max_files`` of the newest-looking ones and normalizes them through FC-03
(``request_logs``). Identical bytes are a no-op. 401/403/robots/CAPTCHA are
recorded BLOCKED and never evaded.

Every source gets a ``published_log_sources`` row (state, files found and
ingested) so coverage of "which agencies publish logs" is evidenced.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

LOG_EXTENSIONS = (".csv", ".tsv", ".xlsx", ".xls", ".pdf")
DEFAULT_PATTERN = r"\blogs?\b|foia[-_ ]?log|request[-_ ]?log|processing[-_ ]?log"
DEFAULT_MAX_FILES = 6
CADENCES = ("monthly", "quarterly", "semiannual", "annual", "irregular")


@dataclass
class LogSource:
    source_id: str
    agency_slug: str
    index_url: str
    component: Optional[str] = None
    cadence: str = "irregular"
    link_pattern: str = DEFAULT_PATTERN
    max_files: int = DEFAULT_MAX_FILES
    verified: bool = False
    notes: str = ""
    extra_index_urls: List[str] = field(default_factory=list)


def load_registry(path: str | Path) -> List[LogSource]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    sources = []
    seen = set()
    for raw in data.get("sources") or []:
        index_url = str(raw.get("index_url") or "").strip()
        agency_slug = str(raw.get("agency_slug") or "").strip()
        if not index_url.startswith("https://") or not agency_slug:
            raise ValueError(f"published log source needs https index_url and agency_slug: {raw!r}")
        cadence = str(raw.get("cadence") or "irregular")
        if cadence not in CADENCES:
            raise ValueError(f"unknown cadence {cadence!r} for {index_url}")
        source_id = str(raw.get("id") or hashlib.sha256(index_url.encode()).hexdigest()[:12])
        if source_id in seen:
            raise ValueError(f"duplicate published log source id {source_id}")
        seen.add(source_id)
        pattern = str(raw.get("link_pattern") or DEFAULT_PATTERN)
        re.compile(pattern)
        sources.append(LogSource(
            source_id=source_id, agency_slug=agency_slug, index_url=index_url,
            component=raw.get("component"), cadence=cadence, link_pattern=pattern,
            max_files=int(raw.get("max_files") or DEFAULT_MAX_FILES),
            verified=bool(raw.get("verified")), notes=str(raw.get("notes") or ""),
            extra_index_urls=[str(u) for u in raw.get("extra_index_urls") or []],
        ))
    return sources


_YEAR = re.compile(r"(?:19|20)\d{2}")


def _recency_key(url: str, text: str) -> Tuple:
    years = [int(y) for y in _YEAR.findall(url + " " + text)]
    quarter = re.search(r"\bq([1-4])\b|quarter[-_ ]?([1-4])", (url + " " + text).lower())
    q = int(next(g for g in quarter.groups() if g)) if quarter else 0
    return (max(years) if years else 0, q)


def find_log_links(html: str, base_url: str, pattern: str = DEFAULT_PATTERN) -> List[Dict[str, str]]:
    """Log-file links on an index page, newest-looking first (deduplicated)."""
    regex = re.compile(pattern, re.IGNORECASE)
    soup = BeautifulSoup(html or "", "html.parser")
    found: Dict[str, Dict[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"].strip())
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.scheme != "http":
            continue
        path = parsed.path.lower()
        if not path.endswith(LOG_EXTENSIONS) and "/media/" not in path and "/download" not in path:
            continue
        text = " ".join(anchor.get_text(" ").split())
        context = (anchor.find_parent(["li", "tr", "p"]) or anchor).get_text(" ")
        if not (regex.search(url) or regex.search(text) or regex.search(context)):
            continue
        found.setdefault(url.split("#", 1)[0], {"url": url.split("#", 1)[0], "text": text[:300]})
    return sorted(found.values(), key=lambda link: _recency_key(link["url"], link["text"]), reverse=True)


def _record_source(conn: sqlite3.Connection, source: LogSource, state: str, detail: str,
                   files_found: int, files_ingested: int) -> None:
    conn.execute(
        """INSERT INTO published_log_sources(source_id,agency_slug,component,index_url,cadence,state,
               detail,files_found,files_ingested,last_checked_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET agency_slug=excluded.agency_slug,
               component=excluded.component, index_url=excluded.index_url, cadence=excluded.cadence,
               state=excluded.state, detail=excluded.detail, files_found=excluded.files_found,
               files_ingested=excluded.files_ingested, last_checked_at=excluded.last_checked_at""",
        (source.source_id, source.agency_slug, source.component, source.index_url, source.cadence,
         state, detail[:500], files_found, files_ingested, datetime.now(timezone.utc).isoformat()),
    )


def scrape_source(
    conn: sqlite3.Connection,
    source: LogSource,
    fetch_page: Callable[[str], Tuple[int, str, str]],
    fetch_file: Callable[[str], Tuple[int, str, bytes]],
    *,
    dry_run: bool = False,
    max_files: Optional[int] = None,
) -> Dict[str, Any]:
    """Scrape one source. ``fetch_page``/``fetch_file`` return (status, final_url, body)."""
    from .closed_requests import create_tables
    from .request_logs import RequestLogError, normalize_request_log, store_request_log
    from .scraper_core import SourceAccessBlocked

    create_tables(conn)
    result: Dict[str, Any] = {"source_id": source.source_id, "agency_slug": source.agency_slug,
                              "index_url": source.index_url, "files": []}
    links: List[Dict[str, str]] = []
    try:
        for index_url in [source.index_url, *source.extra_index_urls]:
            status, final_url, html = fetch_page(index_url)
            if status == 404:
                continue
            if re.search(r"captcha|are you a robot|attention required", html[:20000], re.IGNORECASE):
                raise SourceAccessBlocked(f"visitor verification on {final_url}; not evaded")
            links.extend(find_log_links(html, final_url, source.link_pattern))
    except SourceAccessBlocked as exc:
        result.update(state="BLOCKED", detail=str(exc))
        with conn:
            _record_source(conn, source, "BLOCKED", str(exc), 0, 0)
        return result
    except Exception as exc:  # outage/DNS/timeout: record, keep going with other sources
        result.update(state="FAILED", detail=f"{type(exc).__name__}: {exc}")
        with conn:
            _record_source(conn, source, "FAILED", result["detail"], 0, 0)
        return result

    unique = list({link["url"]: link for link in links}.values())
    limit = max_files if max_files is not None else source.max_files
    ingested = 0
    for link in unique[:limit]:
        entry = {"url": link["url"], "text": link["text"]}
        if dry_run:
            entry["state"] = "found"
            result["files"].append(entry)
            continue
        try:
            status, final_url, body = fetch_file(link["url"])
            if status == 404 or not body:
                entry["state"] = "missing"
            else:
                log = normalize_request_log(body, source_url=final_url, filename=urlparse(final_url).path,
                                            agency=source.agency_slug)
                stored = store_request_log(conn, log)
                entry.update(state="ingested" if stored else "unchanged", rows=len(log.rows),
                             failures=len(log.failures), format=log.file_format)
                ingested += int(stored)
        except SourceAccessBlocked as exc:
            entry.update(state="BLOCKED", detail=str(exc))
        except RequestLogError as exc:
            entry.update(state="unparsed", detail=str(exc))
        except Exception as exc:
            entry.update(state="failed", detail=f"{type(exc).__name__}: {exc}"[:300])
        result["files"].append(entry)
    state = "OK" if unique else "NO_LOG_LINKS"
    detail = f"{len(unique)} log links; {ingested} new logs ingested"
    result.update(state=state, detail=detail, files_found=len(unique), files_ingested=ingested)
    with conn:
        _record_source(conn, source, state, detail, len(unique), ingested)
    return result


def published_log_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from .closed_requests import create_tables

    create_tables(conn)
    return [dict(row) for row in conn.execute(
        "SELECT * FROM published_log_sources ORDER BY agency_slug, component")]

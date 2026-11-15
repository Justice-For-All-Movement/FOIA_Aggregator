"""FC-06: adaptive, read-only adapters for sitemaps and JSON listing APIs.

Adapters only parse what a permitted public interface returned; fetching
(robots, rate limits, safe redirects) stays with the caller. Challenge pages
and persistent denials raise ``SourceAccessBlocked`` so FC-02 records the
source as BLOCKED — they are never solved or evaded.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .enumeration import Page, PaginationError, require_same_host
from .scraper_core import SourceAccessBlocked

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_PENDING_SITEMAPS = 5000
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
CHALLENGE_MARKERS = re.compile(
    r"captcha|cf-chl|challenge-platform|are you a robot|verify you are human|access denied|request unsuccessful",
    re.IGNORECASE)


def detect_site_type(url: str, content_type: str, body: bytes) -> str:
    """Classify a fetched public response to pick an adapter."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    head = body[:4096].lstrip()
    text = head.decode("utf-8", errors="replace")
    path = urlsplit(url).path.lower()
    if CHALLENGE_MARKERS.search(text) and ctype in ("text/html", ""):
        return "challenge"
    if head.startswith(b"%PDF-"):
        return "document"
    if ctype in ("application/json", "text/json") or head[:1] in (b"{", b"["):
        return "json_api"
    if b"<urlset" in head or b"<sitemapindex" in head or path.endswith("sitemap.xml"):
        return "sitemap"
    if "readingroom.aspx" in path or "/app/readingroom" in path or "pal" in path.split("/"):
        return "pal"
    if ctype in ("text/html", "application/xhtml+xml") or b"<html" in head.lower():
        return "html_listing"
    return "unknown"


def raise_if_blocked(status: int, content_type: str, body: bytes) -> None:
    if status in (401, 403) or (status == 200 and detect_site_type("", content_type, body) == "challenge"):
        raise SourceAccessBlocked(f"HTTP {status} access control or challenge page; not evaded")


def _parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise PaginationError("Sitemap exceeds size limit")
    if re.search(rb"<!DOCTYPE|<!ENTITY", data, re.IGNORECASE):
        raise PaginationError("Sitemap declares a DTD/entities; refused")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise PaginationError(f"Invalid sitemap XML: {exc}") from None


def parse_sitemap(data: bytes, source_url: str) -> Tuple[str, List[Dict[str, Optional[str]]]]:
    """Return ("urlset", [{loc, lastmod}]) or ("index", [{loc, lastmod}]) — same-host only."""
    root = _parse_xml(data)
    tag = root.tag.replace(SITEMAP_NS, "")
    child = "url" if tag == "urlset" else "sitemap" if tag == "sitemapindex" else None
    if child is None:
        raise PaginationError("Not a sitemap document")
    out = []
    for node in root.findall(f"{SITEMAP_NS}{child}") + root.findall(child):
        loc = (node.findtext(f"{SITEMAP_NS}loc") or node.findtext("loc") or "").strip()
        if not loc:
            continue
        try:
            require_same_host(source_url, loc)
        except PaginationError:
            continue  # foreign hosts are not this source's records
        out.append({"loc": loc, "lastmod": (node.findtext(f"{SITEMAP_NS}lastmod") or node.findtext("lastmod"))})
    return ("urlset" if child == "url" else "index"), out


def sitemap_pager(fetch: Callable[[str], Tuple[int, str, bytes]], source_url: str):
    """Build an FC-02 ``fetch_page`` that walks a sitemap tree breadth-first."""
    def fetch_page(cursor: Dict[str, Any]) -> Page:
        pending = list(cursor.get("pending") or [])
        if not pending:
            return Page([], None)
        url = require_same_host(source_url, pending.pop(0))
        status, ctype, body = fetch(url)
        raise_if_blocked(status, ctype, body)
        if status != 200:
            raise PaginationError(f"HTTP {status} for sitemap")
        kind, entries = parse_sitemap(body, source_url)
        items = []
        if kind == "index":
            pending.extend(e["loc"] for e in entries)
            if len(pending) > MAX_PENDING_SITEMAPS:
                raise PaginationError("Sitemap index fan-out exceeds limit")
        else:
            items = [{"key": e["loc"], "lastmod": e["lastmod"]} for e in entries]
        return Page(items, {"pending": pending} if pending else None)
    return fetch_page


def _dig(payload: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(payload, dict):
            payload = payload.get(part)
        elif isinstance(payload, list) and part.isdigit() and int(part) < len(payload):
            payload = payload[int(part)]
        else:
            return None
    return payload


def parse_json_listing(payload: Any, *, source_url: str, items_path: str, url_field: str,
                       next_path: Optional[str] = None, total_path: Optional[str] = None,
                       base_cursor: Optional[Dict[str, Any]] = None) -> Page:
    """Generic JSON listing → FC-02 Page, for documented public APIs."""
    items = _dig(payload, items_path)
    if not isinstance(items, list):
        raise PaginationError(f"No list at {items_path!r}")
    out = []
    for item in items:
        url = _dig(item, url_field) if isinstance(item, dict) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append({"key": url})
    next_url = _dig(payload, next_path) if next_path else None
    total = _dig(payload, total_path) if total_path else None
    next_cursor = {"url": require_same_host(source_url, next_url)} if isinstance(next_url, str) and next_url else None
    return Page(out, next_cursor, total if isinstance(total, int) and total >= 0 else None)

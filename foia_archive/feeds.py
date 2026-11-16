"""RSS/Atom feed discovery and parsing for reading rooms.

Many agency sites (Drupal, Plone, WordPress) advertise a feed with
``<link rel="alternate" type="application/rss+xml">``. Feed items carry a real
publication date, so a feed is the cheapest structured listing a reading room
offers. Parsing is defensive: size-capped, DTD/entity declarations refused.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin

MAX_FEED_BYTES = 5 * 1024 * 1024
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
ATOM = "{http://www.w3.org/2005/Atom}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"


def discover_feed_urls(soup, base_url: str) -> List[str]:
    """Absolute feed URLs advertised by ``<link rel="alternate">``."""
    urls: List[str] = []
    for tag in soup.find_all("link", href=True):
        rel = {str(value).lower() for value in (tag.get("rel") or [])}
        kind = str(tag.get("type") or "").split(";")[0].strip().lower()
        if "alternate" in rel and kind in FEED_TYPES:
            url = urljoin(base_url, tag["href"])
            if url not in urls:
                urls.append(url)
    return urls


def looks_like_feed(content_type: str, body: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in FEED_TYPES:
        return True
    head = body[:2048].lstrip().lower()
    return ("<rss" in head or "<feed" in head or "<rdf:rdf" in head) and ctype in (
        "", "text/xml", "application/xml", "text/plain")


def _iso_date(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    stamp = None
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        stamp = None
    if stamp is None:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?", value)
        if not match:
            return None
        try:
            stamp = datetime.fromisoformat(match.group(1))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp.year < 1990 or stamp > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return stamp.date().isoformat()


def parse_feed(body: str, base_url: str) -> List[Dict[str, Optional[str]]]:
    """Return ``[{url, title, published_date, enclosures: [...]}, ...]``."""
    data = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds size limit")
    if re.search(rb"<!DOCTYPE|<!ENTITY", data[:4096], re.IGNORECASE):
        raise ValueError("Feed declares a DTD/entities; refused")
    root = ET.fromstring(data)
    items: List[Dict[str, Optional[str]]] = []

    def text(node, *names):
        for name in names:
            found = node.find(name)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return None

    for node in root.iter():
        tag = node.tag
        if tag in ("item", "{http://purl.org/rss/1.0/}item"):
            link = text(node, "link", "{http://purl.org/rss/1.0/}link", "guid")
            date = text(node, "pubDate", DC_DATE, "{http://purl.org/rss/1.0/}date")
            enclosures = [urljoin(base_url, enc.get("url")) for enc in node.findall("enclosure") if enc.get("url")]
            title = text(node, "title", "{http://purl.org/rss/1.0/}title")
        elif tag == f"{ATOM}entry":
            link = None
            enclosures = []
            for candidate in node.findall(f"{ATOM}link"):
                rel = candidate.get("rel") or "alternate"
                if rel == "alternate" and not link:
                    link = candidate.get("href")
                elif rel == "enclosure" and candidate.get("href"):
                    enclosures.append(urljoin(base_url, candidate.get("href")))
            date = text(node, f"{ATOM}published", f"{ATOM}updated", DC_DATE)
            title = text(node, f"{ATOM}title")
        else:
            continue
        if not link or not link.strip():
            continue
        items.append({
            "url": urljoin(base_url, link.strip()),
            "title": title,
            "published_date": _iso_date(date),
            "enclosures": enclosures,
        })
    return items

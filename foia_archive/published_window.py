"""Published-date window for crawls ("everything released in the last week").

A window is configured as ``crawler.published_window`` with ISO dates
``since``/``until`` (inclusive) and ``include_unknown`` (default false).
While a window is active the crawler:

* still records metadata for every discovered candidate (so coverage and
  dates stay evidenced),
* downloads/archives only candidates whose detected published date is inside
  the window (or whose date is unknown when ``include_unknown`` is true),
* lets only admitted candidates consume the per-source document quota, and
* processes in-window links on a page before unknown/out-of-window links.

Counts are kept in ``config.data["_published_window_stats"]``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

IN_WINDOW = "in_window"
NEWLY_FOUND = "newly_found"
UNKNOWN = "unknown_date"
OUT_OF_WINDOW = "out_of_window"
STATS_KEY = "_published_window_stats"


def parse_iso_date(value: Any, label: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


@dataclass(frozen=True)
class PublishedWindow:
    since: Optional[str]
    until: Optional[str]
    include_unknown: bool = False

    def classify(self, published_date: Optional[str]) -> str:
        if not published_date:
            return UNKNOWN
        day = str(published_date)[:10]
        if self.since and day < self.since:
            return OUT_OF_WINDOW
        if self.until and day > self.until:
            return OUT_OF_WINDOW
        return IN_WINDOW

    def admits(self, published_date: Optional[str]) -> bool:
        verdict = self.classify(published_date)
        return verdict == IN_WINDOW or (verdict == UNKNOWN and self.include_unknown)

    def classify_record(self, published_date: Optional[str], newly_found_at: Optional[str]) -> str:
        """Dated in window wins; otherwise "newly found" (first seen after the
        source's baseline crawl, within the window) is a release proxy."""
        verdict = self.classify(published_date)
        if verdict != IN_WINDOW and newly_found_at and self.classify(newly_found_at) == IN_WINDOW:
            return NEWLY_FOUND
        return verdict

    def admits_record(self, published_date: Optional[str], newly_found_at: Optional[str]) -> bool:
        verdict = self.classify_record(published_date, newly_found_at)
        return verdict in (IN_WINDOW, NEWLY_FOUND) or (verdict == UNKNOWN and self.include_unknown)

    def as_dict(self) -> Dict[str, Any]:
        return {"since": self.since, "until": self.until, "include_unknown": self.include_unknown}


def resolve_window(
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    since_days: Optional[int] = None,
    include_unknown: bool = False,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Build the config mapping for a window, or None when no bound is given."""
    if since_days is not None:
        if since:
            raise ValueError("Use either --since-days or --published-since, not both")
        if type(since_days) is not int or not 0 <= since_days <= 3660:
            raise ValueError("--since-days must be between 0 and 3660")
        today = today or datetime.now(timezone.utc).date()
        since = (today - timedelta(days=since_days)).isoformat()
    since = parse_iso_date(since, "published since")
    until = parse_iso_date(until, "published until")
    if since is None and until is None:
        return None
    if since and until and since > until:
        raise ValueError("published since must not be after published until")
    return {"since": since, "until": until, "include_unknown": bool(include_unknown)}


def window_from_config(config) -> Optional[PublishedWindow]:
    raw = (config.crawler or {}).get("published_window")
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("crawler.published_window must be a mapping")
    since = parse_iso_date(raw.get("since"), "crawler.published_window.since")
    until = parse_iso_date(raw.get("until"), "crawler.published_window.until")
    if since is None and until is None:
        return None
    if since and until and since > until:
        raise ValueError("crawler.published_window since is after until")
    include_unknown = raw.get("include_unknown", False)
    if type(include_unknown) is not bool:
        raise ValueError("crawler.published_window.include_unknown must be boolean")
    return PublishedWindow(since, until, include_unknown)


def record(config, verdict: str, *, admitted: bool) -> None:
    stats = config.data.setdefault(STATS_KEY, {
        IN_WINDOW: 0, NEWLY_FOUND: 0, UNKNOWN: 0, OUT_OF_WINDOW: 0, "admitted": 0, "skipped_download": 0,
    })
    stats[verdict] = stats.get(verdict, 0) + 1
    stats["admitted" if admitted else "skipped_download"] += 1


def stats(config) -> Dict[str, int]:
    return dict(config.data.get(STATS_KEY) or {
        IN_WINDOW: 0, NEWLY_FOUND: 0, UNKNOWN: 0, OUT_OF_WINDOW: 0, "admitted": 0, "skipped_download": 0,
    })


def priority(window: Optional[PublishedWindow], published_date: Optional[str]) -> int:
    """Sort key: in-window first, then unknown, then out-of-window."""
    if window is None:
        return 0
    return {IN_WINDOW: 0, UNKNOWN: 1, OUT_OF_WINDOW: 2}[window.classify(published_date)]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

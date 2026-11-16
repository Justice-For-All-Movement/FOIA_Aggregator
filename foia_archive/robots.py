"""robots.txt compliance (RFC 9309) for every crawler request.

One cached policy per scheme+host per process. A 4xx robots.txt means no
restrictions; an unreachable robots.txt or a 5xx means complete disallow for
this run (RFC 9309 section 2.3.1.4) and the source is recorded as BLOCKED,
never evaded. ``Crawl-delay`` raises the per-host delay (capped).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple
from urllib import robotparser
from urllib.parse import urlsplit

import requests

MAX_ROBOTS_BYTES = 512 * 1024
MAX_CRAWL_DELAY_SECONDS = 60.0
CACHE_SECONDS = 6 * 3600


class RobotsPolicy:
    def __init__(self, fetch: Optional[Callable[[str, str], Tuple[int, str]]] = None):
        self._fetch = fetch or _fetch_robots
        self._lock = threading.Lock()
        self._host_locks: Dict[str, threading.Lock] = {}
        self._cache: Dict[str, Tuple[float, Optional[robotparser.RobotFileParser], bool]] = {}
        self._why: Dict[str, str] = {}

    def _entry(self, url: str, user_agent: str):
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        with self._lock:
            host_lock = self._host_locks.setdefault(origin, threading.Lock())
        with host_lock:
            cached = self._cache.get(origin)
            if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached
            parser: Optional[robotparser.RobotFileParser] = None
            unreachable = False
            why = "robots.txt rule"
            try:
                status, text = self._fetch(origin + "/robots.txt", user_agent)
                if 200 <= status < 300:
                    parser = robotparser.RobotFileParser()
                    parser.parse(text.splitlines())
                elif status >= 500 or status == 429:
                    unreachable = True
                    why = f"robots.txt unavailable (HTTP {status})"
                # Other 4xx: no robots.txt, no restrictions.
            except Exception as exc:  # noqa: BLE001 - unreachable means disallow
                unreachable = True
                why = f"robots.txt unreachable ({type(exc).__name__})"
            self._why[origin] = why
            entry = (time.monotonic(), parser, unreachable)
            self._cache[origin] = entry
            return entry

    def allowed(self, url: str, user_agent: str) -> bool:
        _, parser, unreachable = self._entry(url, user_agent)
        if unreachable:
            return False
        if parser is None:
            return True
        return parser.can_fetch(_token(user_agent), url)

    def explain(self, url: str) -> str:
        parts = urlsplit(url)
        return self._why.get(f"{parts.scheme.lower()}://{parts.netloc.lower()}", "robots.txt rule")

    def crawl_delay(self, url: str, user_agent: str) -> Optional[float]:
        _, parser, _ = self._entry(url, user_agent)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(_token(user_agent))
        except Exception:  # noqa: BLE001
            return None
        if delay is None:
            return None
        try:
            return min(MAX_CRAWL_DELAY_SECONDS, max(0.0, float(delay)))
        except (TypeError, ValueError):
            return None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


def _token(user_agent: str) -> str:
    """Product token used to match robots.txt groups (e.g. FOIAArchiveBot)."""
    return (user_agent or "*").split("/", 1)[0].strip() or "*"


def _fetch_robots(url: str, user_agent: str) -> Tuple[int, str]:
    from .scraper_core import _validate_public_destination

    current = url
    for _ in range(5):
        _validate_public_destination(current)
        response = requests.get(current, headers={"User-Agent": user_agent}, timeout=15,
                                allow_redirects=False, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                from urllib.parse import urljoin
                current = urljoin(current, response.headers["Location"])
                continue
            body = b""
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) > MAX_ROBOTS_BYTES:
                    break
            return response.status_code, body[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
        finally:
            response.close()
    return 404, ""


POLICY = RobotsPolicy()


def policy_for(config) -> Optional[RobotsPolicy]:
    """The shared policy when ``crawler.respect_robots_txt`` is enabled."""
    crawler = getattr(config, "crawler", None) or {}
    value = crawler.get("respect_robots_txt", False)
    if isinstance(value, str):
        value = value.strip().lower() in {"1", "true", "yes", "on"}
    return POLICY if value else None

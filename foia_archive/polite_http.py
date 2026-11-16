"""Small, honest JSON/text client for public APIs (MuckRock, probes).

* Always sends the crawler's honest ``user_agent`` (never the browser
  fallback): public APIs are not retried with disguised headers.
* Reuses the crawler's SSRF-safe redirect walker and per-host rate limiter.
* 401/403 become ``SourceAccessBlocked`` immediately (documented BLOCKED,
  never evaded). 429/5xx honour ``Retry-After`` within a bounded budget.
* Response bodies are size-capped before JSON decoding.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

import requests

from .scraper_core import (
    RETRYABLE_STATUS_CODES,
    HostRateLimiter,
    SourceAccessBlocked,
    _close_response,
    _read_limited_text,
    _request_headers,
    _request_with_safe_redirects,
    _retry_sleep,
)
from .utils import Config

DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def fetch_text(
    url: str,
    config: Config,
    rate_limiter: HostRateLimiter,
    *,
    token: Optional[str] = None,
    accept: str = "application/json",
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_retries: int = 2,
    max_retry_delay: float = 30.0,
) -> Tuple[int, Dict[str, str], str, str]:
    """Return ``(status, headers, final_url, text)`` for a 2xx/404 response."""
    headers = _request_headers(config)
    headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Token {token}"
    timeout = float(config.crawler.get("page_timeout_seconds", 30))
    for attempt in range(max_retries + 1):
        response = None
        try:
            response, final_url = _request_with_safe_redirects(
                url, headers=headers, timeout=timeout, max_redirects=3,
                rate_limiter=rate_limiter,
            )
            status = int(getattr(response, "status_code", 200))
            response_headers = dict(getattr(response, "headers", {}) or {})
            if status in (401, 403):
                raise SourceAccessBlocked(f"HTTP {status} from {final_url}")
            if status in RETRYABLE_STATUS_CODES and attempt < max_retries:
                _retry_sleep(
                    attempt, 2.0,
                    retry_after=response_headers.get("Retry-After") or response_headers.get("retry-after"),
                    max_delay_seconds=max_retry_delay,
                )
                continue
            if status == 404 or 200 <= status < 300:
                return status, response_headers, final_url, _read_limited_text(response, max_bytes)
            raise requests.HTTPError(f"HTTP {status} from {final_url}")
        finally:
            if response is not None:
                _close_response(response)
    raise requests.HTTPError(f"Retries exhausted for {url}")


def fetch_json(
    url: str,
    config: Config,
    rate_limiter: HostRateLimiter,
    *,
    token: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    status, _, final_url, text = fetch_text(url, config, rate_limiter, token=token, max_bytes=max_bytes)
    if status != 200:
        raise requests.HTTPError(f"HTTP {status} from {final_url}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload

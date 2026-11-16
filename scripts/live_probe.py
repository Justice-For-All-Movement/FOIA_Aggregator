"""Bounded, read-only live probe (CI only; no secrets, no B2, no downloads).

Sections:
  muckrock-discovery  record the real MuckRock API shape (keys, date fields,
                      filters, rate-limit headers, robots lines).
Output is a plain-text summary in the job log (and GITHUB_STEP_SUMMARY).
Every request uses the crawler's honest User-Agent, SSRF-safe redirects and
a per-host delay. Personal fields are never printed: only key names, ids,
public titles, statuses and dates.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foia_archive.polite_http import fetch_text  # noqa: E402
from foia_archive.scraper_core import HostRateLimiter, SourceAccessBlocked  # noqa: E402
from foia_archive.utils import load_config  # noqa: E402

LINES: list[str] = []
SAFE_VALUE_KEYS = {"id", "title", "status", "slug", "count", "next", "previous"}
PERSONAL_HINTS = ("user", "email", "requester", "address", "phone", "contact", "name")


def out(line: str = "") -> None:
    print(line, flush=True)
    LINES.append(line)


def _datey(key: str) -> bool:
    k = key.lower()
    return any(t in k for t in ("date", "datetime", "updated", "submitted", "created", "done", "due"))


def describe_item(item: dict, prefix: str = "  item") -> None:
    out(f"{prefix} keys: {sorted(item)}")
    shown = {}
    for key, value in item.items():
        lowered = key.lower()
        if _datey(key) or key in SAFE_VALUE_KEYS:
            if not any(h in lowered for h in PERSONAL_HINTS) or key == "title":
                shown[key] = value if not isinstance(value, (dict, list)) else type(value).__name__
        elif key in ("agency",):
            shown[key] = value if isinstance(value, (int, str)) else type(value).__name__
    out(f"{prefix} safe values: {json.dumps(shown, default=str)[:600]}")


def probe(url: str, cfg, limiter, *, accept: str = "application/json") -> dict | None:
    try:
        status, headers, final_url, text = fetch_text(url, cfg, limiter, accept=accept, max_bytes=3_000_000, return_blocked=True)
    except SourceAccessBlocked as exc:
        out(f"BLOCKED {url}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        out(f"ERROR {url}: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    rl = {k: v for k, v in headers.items() if "ratelimit" in k.lower() or k.lower() in ("retry-after", "content-type", "allow")}
    out(f"GET {url} -> {status} final={final_url} bytes={len(text)} headers={rl}")
    if status in (401, 403):
        server = {k: v for k, v in headers.items() if k.lower() in ("server", "cf-mitigated", "www-authenticate", "cf-ray")}
        out(f"  BLOCKED {status} server-headers={server}")
        out("  body head: " + " ".join(text[:400].split()))
        return None
    if "json" not in (headers.get("Content-Type") or headers.get("content-type") or "") or status != 200:
        return {"_text": text, "_status": status}
    try:
        payload = json.loads(text)
    except ValueError:
        out("  (not JSON)")
        return {"_text": text, "_status": status}
    if isinstance(payload, dict):
        out(f"  top keys: {sorted(payload)[:40]}")
        for key in ("count", "next", "previous"):
            if key in payload:
                out(f"  {key}: {payload[key]}")
        results = payload.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            describe_item(results[0])
            for extra in results[1:3]:
                if isinstance(extra, dict):
                    out("  other dates: " + json.dumps({k: v for k, v in extra.items() if _datey(k)}, default=str)[:300])
    return payload if isinstance(payload, dict) else {"_list": payload}


def muckrock_discovery(cfg) -> None:
    out("## MuckRock API discovery")
    limiter = HostRateLimiter(3.0)
    robots = probe("https://www.muckrock.com/robots.txt", cfg, limiter, accept="text/plain")
    if robots and "_text" in robots:
        for line in robots["_text"].splitlines():
            if line.strip() and not line.startswith("#"):
                out(f"  robots: {line.strip()[:160]}")
    probe("https://www.muckrock.com/api_v1/foia/?format=json&page_size=2", cfg, limiter)
    root = probe("https://www.muckrock.com/api_v2/", cfg, limiter)
    probe("https://www.muckrock.com/api_v2/requests/?page_size=2", cfg, limiter)
    probe("https://www.muckrock.com/api_v2/requests/?page_size=2&ordering=-datetime_updated", cfg, limiter)
    probe("https://www.muckrock.com/api_v2/requests/?page_size=2&datetime_submitted__gte=2026-11-01", cfg, limiter)
    schema = None
    for candidate in ("https://www.muckrock.com/api_v2/schema/?format=json",
                      "https://www.muckrock.com/api_v2/schema/"):
        schema = probe(candidate, cfg, limiter, accept="application/vnd.oai.openapi+json, application/json")
        if schema and "paths" in schema:
            break
    if schema and isinstance(schema.get("paths"), dict):
        for path, ops in schema["paths"].items():
            if "request" in path or "foia" in path or "communication" in path:
                get = (ops or {}).get("get") or {}
                params = [p.get("name") for p in get.get("parameters", []) if isinstance(p, dict)]
                out(f"  schema {path}: params={params[:60]}")
        sec = schema.get("components", {}).get("securitySchemes")
        out(f"  securitySchemes: {json.dumps(sec)[:300]}")
    elif schema and "_text" in schema:
        out("  schema text head: " + schema["_text"][:300].replace("\n", " "))
    if root and "_text" not in root:
        out(f"  api_v2 root: {json.dumps(root)[:500]}")


MUCKROCK_SOURCE_FILES = (
    "muckrock/foia/api_v2/viewsets.py",
    "muckrock/foia/api_v2/serializers.py",
    "muckrock/foia/api_v2/filters.py",
    "muckrock/core/api_v2/pagination.py",
    "muckrock/settings/base.py",
)
SOURCE_HINTS = ("filter", "ordering", "fields", "class ", "permission", "throttl", "datetime",
                "page_size", "cursor", "authentication", "lookup_expr", "status", "router", "DEFAULT_")


def muckrock_source(cfg) -> None:
    """Read MuckRock's public open-source API definition (raw GitHub files)."""
    out("## MuckRock API v2 shape from public source (github.com/MuckRock/muckrock)")
    limiter = HostRateLimiter(1.0)
    for path in MUCKROCK_SOURCE_FILES:
        url = f"https://raw.githubusercontent.com/MuckRock/muckrock/master/{path}"
        try:
            status, _, _, text = fetch_text(url, cfg, limiter, accept="text/plain", max_bytes=600_000)
        except Exception as exc:  # noqa: BLE001
            out(f"ERROR {path}: {type(exc).__name__}")
            continue
        out(f"### {path} -> {status} ({len(text)} bytes)")
        if status != 200:
            continue
        settings_file = path.endswith("settings/base.py")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if settings_file and not any(t in stripped for t in ("REST_FRAMEWORK", "THROTTLE", "PAGE_SIZE", "DEFAULT_", "rest_framework")):
                continue
            if settings_file or any(t in stripped.lower() for t in SOURCE_HINTS):
                out(f"  {number:4}: {stripped[:170]}")


def main() -> None:
    cfg = load_config(os.getenv("FOIA_CONFIG", "config/settings.yaml"))
    sections = sys.argv[1:] or ["muckrock-discovery"]
    for section in sections:
        if section == "muckrock-discovery":
            muckrock_discovery(cfg)
        elif section == "muckrock-source":
            muckrock_source(cfg)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("```\n" + "\n".join(LINES) + "\n```\n")


if __name__ == "__main__":
    main()

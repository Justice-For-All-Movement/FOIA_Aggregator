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


# (path, verbatim line ranges); other lines are filtered by SOURCE_HINTS.
MUCKROCK_SOURCE_FILES = (
    ("muckrock/foia/api_v2/viewsets.py", ((40, 60),)),
    ("muckrock/foia/api_v2/serializers.py", ((48, 118), (282, 300), (342, 375))),
    ("muckrock/core/pagination.py", ((1, 200),)),
    ("muckrock/settings/base.py", ()),
    ("muckrock/urls.py", ()),
)
SOURCE_HINTS = ("filter", "ordering", "fields", "class ", "permission", "throttl", "datetime",
                "page_size", "cursor", "authentication", "lookup_expr", "status", "router", "DEFAULT_")


def muckrock_source(cfg) -> None:
    """Read MuckRock's public open-source API definition (raw GitHub files)."""
    out("## MuckRock API v2 shape from public source (github.com/MuckRock/muckrock)")
    limiter = HostRateLimiter(1.0)
    for path, verbatim in MUCKROCK_SOURCE_FILES:
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
            if any(lo <= number <= hi for lo, hi in verbatim):
                out(f"  {number:4}| {line.rstrip()[:170]}")
                continue
            if path.endswith("urls.py"):
                if "api" in stripped.lower() or "router" in stripped.lower():
                    out(f"  {number:4}: {stripped[:170]}")
                continue
            if settings_file and not any(t in stripped for t in ("REST_FRAMEWORK", "THROTTLE", "PAGE_SIZE", "DEFAULT_", "rest_framework")):
                continue
            if settings_file or any(t in stripped.lower() for t in SOURCE_HINTS):
                out(f"  {number:4}: {stripped[:170]}")


WINDOW_PROBE_SOURCES = (
    ("https://www.justice.gov/oip/foia-library", "DOJ OIP FOIA library"),
    ("https://www.cia.gov/readingroom/", "CIA reading room"),
    ("https://vault.fbi.gov/", "FBI Vault"),
    ("https://www.dhs.gov/foia-library", "DHS FOIA library"),
    ("https://www.nsa.gov/Helpful-Links/NSA-FOIA/Reading-Room/", "NSA reading room"),
    ("https://www.archives.gov/foia/electronic-reading-room", "NARA reading room"),
    ("https://foia.state.gov/", "State FOIA virtual reading room"),
    ("https://www.dol.gov/general/foia/readroom", "DOL reading room"),
    ("https://www.epa.gov/foia/epa-foia-reading-room", "EPA reading room"),
    ("https://www.odni.gov/accountability/", "ODNI accountability"),
)


def window_crawl(cfg_path: str, days: int) -> None:
    """Dry-run (metadata only) window crawl through the real engine path."""
    import tempfile

    import yaml

    from foia_archive.engine import run_once
    from foia_archive.published_window import resolve_window
    from foia_archive.storage import get_connection, init_db, upsert_reading_room

    out(f"## Published-window crawl (dry run, last {days} days; also reports 30-day counts)")
    with tempfile.TemporaryDirectory() as tmp:
        data = yaml.safe_load(Path(cfg_path).read_text())
        data["storage"].update({"backend": "local", "db_path": f"{tmp}/probe.db", "files_dir": f"{tmp}/files"})
        data["crawler"].update({
            "dry_run": True, "max_pages_per_source": 3, "max_depth": 1,
            "max_discovered_docs_per_source": 300, "per_host_delay_seconds": 2.0,
            "page_timeout_seconds": 20, "page_max_size_mb": 3, "maximum_cycle_seconds": 600,
            # Probe stays honest: never retry a 403 with browser-like headers.
            "browser_compatibility_fallback": False,
        })
        data["downloader"].update({"max_retries": 1})
        data["database_backup"] = {"enabled": False}
        config_file = Path(tmp) / "probe.yaml"
        config_file.write_text(yaml.safe_dump(data))
        init_db(f"{tmp}/probe.db", f"{tmp}/files")
        conn = get_connection(f"{tmp}/probe.db")
        for url, label in WINDOW_PROBE_SOURCES:
            upsert_reading_room(conn, url, label, "agency", None, None, source_type="reading_room")
        conn.close()
        window = resolve_window(since_days=days)
        stats: dict = {}
        run_once(config_path=str(config_file), dry_run=True, max_docs_per_source=50,
                 refresh_metadata_enabled=False, backup_enabled=False,
                 published_window=window, window_stats_out=stats)
        out(f"window={window} engine counts={json.dumps(stats, sort_keys=True)}")
        conn = get_connection(f"{tmp}/probe.db")
        since30 = resolve_window(since_days=30)["since"]
        try:
            for row in conn.execute(
                """SELECT rr.label, rr.url, rr.last_error,
                          COUNT(d.id) AS docs,
                          SUM(CASE WHEN d.published_date IS NOT NULL AND d.published_date != '' THEN 1 ELSE 0 END) AS dated,
                          SUM(CASE WHEN d.published_date >= ? THEN 1 ELSE 0 END) AS in_window,
                          SUM(CASE WHEN d.published_date >= ? THEN 1 ELSE 0 END) AS in_30,
                          MAX(d.published_date) AS newest
                   FROM reading_rooms rr LEFT JOIN documents d ON d.reading_room_id = rr.id
                   GROUP BY rr.id ORDER BY rr.id""",
                (window["since"], since30),
            ):
                state = "FAILED: " + str(row["last_error"])[:140] if row["last_error"] else "reached"
                out(f"  {row['label']}: {state}; docs={row['docs']} dated={row['dated'] or 0} "
                    f"in_{days}d={row['in_window'] or 0} in_30d={row['in_30'] or 0} newest={row['newest']}")
            for row in conn.execute(
                "SELECT title, url, published_date FROM documents WHERE published_date >= ? "
                "ORDER BY published_date DESC LIMIT 8", (window["since"],)):
                out(f"    recent: {row['published_date']} {str(row['title'])[:80]} {row['url'][:120]}")
        finally:
            conn.close()


def main() -> None:
    cfg = load_config(os.getenv("FOIA_CONFIG", "config/settings.yaml"))
    sections = sys.argv[1:] or ["muckrock-discovery"]
    for section in sections:
        if section.startswith("window-crawl"):
            days = int(section.partition(":")[2] or 7)
            try:
                window_crawl(os.getenv("FOIA_CONFIG", "config/settings.yaml"), days)
            except Exception as exc:  # noqa: BLE001
                out(f"window crawl probe error: {type(exc).__name__}: {str(exc)[:300]}")
            continue
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

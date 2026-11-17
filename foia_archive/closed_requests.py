"""Master list of closed FOIA requests, release matching and coverage.

The master list is built from normalized request-log rows (FC-03). One row per
agency + tracking number, keeping the best-evidenced log row. Each request gets:

* a normalized ``disposition`` (``full_grant``, ``partial_grant``, ...);
* a ``first_party`` reason when the request is for the requester's own records
  (A-files, personnel/medical/claims files, ...). Those are never released to
  third parties, so they are excluded from the release denominator;
* ``releasable`` = released in full or in part and not first-party. That is the
  denominator: records an agency released in the period.

Matches record *where* released records for a request are held, with method and
evidence: ``reading_room`` (a document in this archive), ``muckrock``
(a public MuckRock request with the same agency tracking id) or
``documentcloud`` (a public DocumentCloud document naming the tracking number).
Only ``reading_room`` matches count as held by the aggregator; the other two
are "located elsewhere" and stay distinct.

No match is a guess: tracking numbers must be specific (letters/digits with at
least one digit and six characters after normalization) and agency-consistent
where both sides name an agency.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

GAP_EXPORT_FORMAT = "foia-release-gaps/1"
RELEASING = ("full_grant", "partial_grant")
MATCH_SOURCES = ("reading_room", "muckrock", "documentcloud")
MIN_TRACKING_CHARS = 6

# Ordered: first pattern that matches wins. Matched on a lower-cased,
# punctuation-collapsed disposition string.
DISPOSITION_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("partial_grant", ("partial grant", "granted in part", "partially granted", "partial release",
                       "released in part", "partial denial", "partially denied", "denied in part",
                       "part grant", "partial")),
    ("full_grant", ("full grant", "granted in full", "release in full", "released in full",
                    "full release", "grant in full", "granted", "full disclosure", "released")),
    ("full_denial", ("full denial", "denied in full", "withheld in full", "denial based on exemption",
                     "full denial based on exemptions", "denied", "denial")),
    ("no_records", ("no records", "no responsive records", "no record")),
    ("withdrawn", ("withdrawn", "withdrew", "request withdrawn")),
    ("referred", ("referral", "referred", "transferred")),
    ("duplicate", ("duplicate",)),
    ("fee_related", ("fee related", "fee-related", "fees", "failure to pay", "fee")),
    ("improper", ("improper foia request", "improper request", "not reasonably described",
                  "not a proper foia request", "not an agency record", "not agency record",
                  "glomar", "neither confirm nor deny", "other reason", "litigation")),
)

# First-party requests: the requester's own records. Never released publicly,
# so excluded from the third-party release denominator. Matched on subject,
# request-type style raw columns and disposition text.
FIRST_PARTY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("a_file", r"\ba[- ]?files?\b|\balien (?:registration )?files?\b|\ba[- ]?(?:number|#|no\.?)\s*\d"),
    ("immigration_record", r"\bimmigration (?:records?|history|file)\b|\bnaturali[sz]ation (?:records?|file|certificate)\b"
                           r"|\bi-94\b|\bentry[ /-]exit\b|\btravel (?:history|records?)\b|\bborder crossing"
                           r"|\btecs\b|\bgreen card\b|\bmy (?:asylum|visa|daca|tps)\b|\brecord of proceedings?\b"),
    ("personnel_record", r"\bofficial personnel (?:file|folder)\b|\bpersonnel (?:file|records?)\b|\bopf\b"
                         r"|\bdd[- ]?214\b|\bmilitary (?:service|personnel) records?\b|\bservice records?\b"),
    ("medical_or_claims", r"\bmedical records?\b|\bc[- ]?file\b|\bclaims? (?:file|folder|records?)\b|\bva claim"
                          r"|\bdisability claim\b|\bbenefits? (?:file|records?)\b"),
    ("tax_or_ssa", r"\bmy (?:tax|earnings|social security)\b|\btax (?:return|transcript|account)s?\b"
                   r"|\bearnings records?\b|\bnumident\b"),
    ("background_investigation", r"\bbackground investigation (?:file|records?)\b|\bmy background investigation\b"
                                 r"|\bsecurity clearance (?:file|records?)\b"),
    ("self", r"\bfirst[- ]party\b|\bprivacy act request\b|\bmy (?:own )?(?:records?|file|case|information)\b"
             r"|\brecords? (?:about|pertaining to|on) (?:myself|me)\b|\bmyself\b"),
)
_FIRST_PARTY_RE = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in FIRST_PARTY_PATTERNS]
REQUEST_TYPE_HEADER = re.compile(r"type|category|privacy|first|third", re.IGNORECASE)
_TRACKING_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_./ ][A-Za-z0-9]+){0,7}")
_SPLIT = re.compile(r"[-_./ ]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()


def normalize_tracking(value: Any) -> Optional[str]:
    """Separator/case-insensitive tracking number; None unless specific enough."""
    norm = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if len(norm) < MIN_TRACKING_CHARS or not re.search(r"\d", norm):
        return None
    return norm


def normalize_disposition(raw: Any) -> str:
    text = _key_text(raw)
    if not text:
        return "unknown"
    for name, needles in DISPOSITION_PATTERNS:
        if any(re.search(r"\b" + re.escape(needle) + r"\b", text) for needle in needles):
            return name
    return "other"


def first_party_reason(subject: Any, raw_cells: Optional[Dict[str, Any]] = None,
                       disposition: Any = None) -> Optional[str]:
    """Return why a request is first-party, or None."""
    texts = [str(subject or "")]
    for header, value in (raw_cells or {}).items():
        if value and REQUEST_TYPE_HEADER.search(str(header)):
            lowered = str(value).lower()
            if "third" in lowered and "party" in lowered:
                return None  # the log itself says third party
            if ("first" in lowered and "party" in lowered) or "privacy act" in lowered:
                return "logged_first_party"
    texts.append(str(disposition or ""))
    joined = " ".join(texts)
    for name, pattern in _FIRST_PARTY_RE:
        if pattern.search(joined):
            return name
    return None


def _parse_pages(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    match = re.fullmatch(r"(\d{1,9})(?:\.0+)?", text)
    return int(match.group(1)) if match else None


def create_tables(conn: sqlite3.Connection) -> None:
    from .request_logs import create_tables as create_log_tables

    create_log_tables(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS closed_requests (
            request_key TEXT PRIMARY KEY,
            agency_slug TEXT,
            component TEXT,
            tracking_number TEXT,
            tracking_norm TEXT,
            received_date TEXT,
            closed_date TEXT,
            subject TEXT,
            disposition_raw TEXT,
            disposition TEXT NOT NULL,
            pages_released INTEGER,
            first_party_reason TEXT,
            releasable INTEGER NOT NULL,
            log_id TEXT NOT NULL,
            log_sheet TEXT,
            log_row_number INTEGER,
            source_url TEXT NOT NULL,
            built_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_requests_agency_closed "
                 "ON closed_requests(agency_slug, closed_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_requests_tracking ON closed_requests(tracking_norm)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS closed_request_matches (
            request_key TEXT NOT NULL,
            match_source TEXT NOT NULL,
            match_ref TEXT NOT NULL,
            method TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            matched_at TEXT NOT NULL,
            PRIMARY KEY (request_key, match_source, match_ref)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS closed_request_lookups (
            request_key TEXT NOT NULL,
            lookup_source TEXT NOT NULL,
            state TEXT NOT NULL,
            hits INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (request_key, lookup_source)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS published_log_sources (
            source_id TEXT PRIMARY KEY,
            agency_slug TEXT NOT NULL,
            component TEXT,
            index_url TEXT NOT NULL,
            cadence TEXT,
            state TEXT NOT NULL,
            detail TEXT,
            files_found INTEGER NOT NULL DEFAULT 0,
            files_ingested INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT NOT NULL
        )"""
    )


def _row_score(row: sqlite3.Row) -> Tuple:
    filled = sum(1 for k in ("closed_date", "disposition", "request_description", "pages_released",
                             "received_date") if row[k])
    return (1 if row["closed_date"] else 0, filled, row["ingested_at"] or "")


def build_master_list(conn: sqlite3.Connection) -> Dict[str, int]:
    """Rebuild ``closed_requests`` from every stored request-log row.

    A row is a *closed* request when it has a closed date or a terminal
    disposition. Rows without a tracking number keep a log/row-scoped key so
    they are counted but can never be matched or followed up.
    """
    create_tables(conn)
    rows = conn.execute(
        """SELECT r.*, l.agency AS log_agency, l.source_url, l.ingested_at
           FROM request_log_rows r JOIN request_logs l USING(log_id)"""
    ).fetchall()
    best: Dict[str, sqlite3.Row] = {}
    for row in rows:
        disposition = normalize_disposition(row["disposition"])
        if not row["closed_date"] and disposition in ("unknown", "other"):
            continue  # open or unknown-status request: not part of the closed list
        agency = (row["log_agency"] or "").strip() or None
        norm = normalize_tracking(row["tracking_number"])
        identity = f"{agency or ''}|{norm}" if norm else f"{row['log_id']}|{row['sheet']}|{row['row_number']}"
        key = "cr_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if key not in best or _row_score(row) > _row_score(best[key]):
            best[key] = row
    stamp = _now()
    records = []
    for key, row in best.items():
        disposition = normalize_disposition(row["disposition"])
        raw = json.loads(row["raw_json"] or "{}")
        reason = first_party_reason(row["request_description"], raw, row["disposition"])
        releasable = int(disposition in RELEASING and reason is None)
        records.append((
            key, (row["log_agency"] or "").strip() or None, row["component"], row["tracking_number"],
            normalize_tracking(row["tracking_number"]), row["received_date"], row["closed_date"],
            row["request_description"], row["disposition"], disposition,
            _parse_pages(row["pages_released"]), reason, releasable, row["log_id"], row["sheet"],
            row["row_number"], row["source_url"], stamp,
        ))
    with conn:
        conn.execute("DELETE FROM closed_requests")
        conn.executemany(
            """INSERT INTO closed_requests(request_key,agency_slug,component,tracking_number,tracking_norm,
                   received_date,closed_date,subject,disposition_raw,disposition,pages_released,
                   first_party_reason,releasable,log_id,log_sheet,log_row_number,source_url,built_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            records,
        )
        # Matches for requests that no longer exist are stale evidence.
        conn.execute("DELETE FROM closed_request_matches WHERE request_key NOT IN "
                     "(SELECT request_key FROM closed_requests)")
    return {
        "log_rows": len(rows),
        "closed_requests": len(records),
        "releasable": sum(r[12] for r in records),
        "first_party": sum(1 for r in records if r[11]),
    }


def _tokens_for_text(text: str) -> Iterable[str]:
    """Normalized tracking-like tokens and their contiguous sub-sequences."""
    for match in _TRACKING_TOKEN.finditer(text or ""):
        parts = [p for p in _SPLIT.split(match.group(0).upper()) if p]
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                norm = normalize_tracking("".join(parts[start:end]))
                if norm:
                    yield norm


def _document_tokens(row: sqlite3.Row) -> set:
    path = unquote(urlparse(row["url"] or "").path)
    texts = [row["title"] or "", row["filename"] or "", path.rsplit("/", 1)[-1]]
    found = set()
    for text in texts:
        found.update(_tokens_for_text(text))
    return found


def _record_match(conn, request_key, source, ref, method, confidence, evidence, stamp) -> None:
    conn.execute(
        """INSERT INTO closed_request_matches(request_key,match_source,match_ref,method,confidence,
               evidence_json,matched_at) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(request_key,match_source,match_ref) DO UPDATE SET
               method=excluded.method, confidence=excluded.confidence,
               evidence_json=excluded.evidence_json, matched_at=excluded.matched_at""",
        (request_key, source, str(ref), method, confidence, json.dumps(evidence, sort_keys=True), stamp),
    )


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def match_reading_room(conn: sqlite3.Connection, *, use_full_text: bool = True,
                       max_text_queries: int = 20_000) -> Dict[str, int]:
    """Match releasable closed requests to documents in this archive."""
    create_tables(conn)
    stamp = _now()
    index: Dict[str, List[Tuple[int, Optional[str]]]] = {}
    docs = conn.execute(
        """SELECT d.id, d.url, d.title, d.filename, a.slug AS agency_slug
           FROM documents d LEFT JOIN agencies a ON a.id = d.agency_id"""
    )
    for row in docs:
        for token in _document_tokens(row):
            bucket = index.setdefault(token, [])
            if len(bucket) < 50:
                bucket.append((row["id"], row["agency_slug"]))
    requests_ = conn.execute(
        """SELECT request_key, agency_slug, tracking_number, tracking_norm FROM closed_requests
           WHERE releasable = 1 AND tracking_norm IS NOT NULL"""
    ).fetchall()
    has_fts = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='document_fts'").fetchone())
    metadata = text = text_queries = 0
    with conn:
        for request in requests_:
            hits = index.get(request["tracking_norm"], [])
            for doc_id, doc_agency in hits:
                if request["agency_slug"] and doc_agency and doc_agency != request["agency_slug"]:
                    continue
                confidence = 0.95 if doc_agency and doc_agency == request["agency_slug"] else 0.8
                _record_match(conn, request["request_key"], "reading_room", doc_id,
                              "tracking_number_in_title_or_filename", confidence,
                              {"tracking_norm": request["tracking_norm"]}, stamp)
                metadata += 1
            if hits or not (use_full_text and has_fts) or text_queries >= max_text_queries:
                continue
            text_queries += 1
            try:
                found = conn.execute(
                    """SELECT f.rowid AS id, a.slug AS agency_slug FROM document_fts f
                       JOIN documents d ON d.id = f.rowid LEFT JOIN agencies a ON a.id = d.agency_id
                       WHERE document_fts MATCH ? LIMIT 20""",
                    (_fts_phrase(str(request["tracking_number"]).strip()),),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for hit in found:
                if request["agency_slug"] and hit["agency_slug"] and hit["agency_slug"] != request["agency_slug"]:
                    continue
                _record_match(conn, request["request_key"], "reading_room", hit["id"],
                              "tracking_number_in_document_text", 0.7,
                              {"phrase": str(request["tracking_number"]).strip()}, stamp)
                text += 1
    return {"requests_checked": len(requests_), "metadata_matches": metadata,
            "text_matches": text, "text_queries": text_queries}


def _agency_compatible(request_agency: Optional[str], name: Optional[str]) -> bool:
    if not request_agency or not name:
        return True
    words = {w for w in _key_text(request_agency.replace("-", " ")).split()
             if w not in {"department", "of", "the", "and", "u", "s", "us", "office", "for"}}
    other = set(_key_text(name).split())
    return not words or bool(words & other)


def match_muckrock(conn: sqlite3.Connection) -> Dict[str, int]:
    """Match on the agency tracking id MuckRock records for public requests."""
    create_tables(conn)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='third_party_requests'").fetchone():
        return {"requests_checked": 0, "matches": 0}
    by_norm: Dict[str, List[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM third_party_requests WHERE tracking_id IS NOT NULL AND tracking_id != ''"
    ):
        for piece in re.split(r"[;,]", row["tracking_id"]):
            norm = normalize_tracking(piece)
            if norm:
                by_norm.setdefault(norm, []).append(row)
    has_releases = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='third_party_releases'").fetchone())
    requests_ = conn.execute(
        """SELECT request_key, agency_slug, tracking_norm FROM closed_requests
           WHERE releasable = 1 AND tracking_norm IS NOT NULL"""
    ).fetchall()
    stamp = _now()
    matches = 0
    with conn:
        for request in requests_:
            for row in by_norm.get(request["tracking_norm"], []):
                if not _agency_compatible(request["agency_slug"], row["agency_name"]):
                    continue
                files = 0
                if has_releases:
                    files = conn.execute(
                        "SELECT COUNT(*) FROM third_party_releases WHERE publisher=? AND request_id=?",
                        (row["publisher"], row["request_id"]),
                    ).fetchone()[0]
                _record_match(conn, request["request_key"], "muckrock", row["public_url"] or row["request_id"],
                              "agency_tracking_id_exact", 0.9,
                              {"status": row["status"], "files_listed": files,
                               "agency_name": row["agency_name"]}, stamp)
                matches += 1
    return {"requests_checked": len(requests_), "matches": matches}


DOCUMENTCLOUD_SEARCH = "https://api.www.documentcloud.org/api/documents/search/"


def match_documentcloud(conn: sqlite3.Connection, fetch_json: Callable[[str], Dict[str, Any]], *,
                        max_queries: int = 200, recheck_days: int = 30) -> Dict[str, int]:
    """Search public DocumentCloud documents for each unmatched tracking number.

    ``fetch_json`` performs one polite GET (robots, rate limit, honest UA).
    Bounded by ``max_queries``; a request is not re-queried for
    ``recheck_days`` after a lookup.
    """
    from urllib.parse import urlencode

    create_tables(conn)
    candidates = conn.execute(
        """SELECT c.request_key, c.agency_slug, c.tracking_number, c.tracking_norm FROM closed_requests c
           WHERE c.releasable = 1 AND c.tracking_norm IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM closed_request_matches m WHERE m.request_key = c.request_key)
             AND NOT EXISTS (SELECT 1 FROM closed_request_lookups k WHERE k.request_key = c.request_key
                             AND k.lookup_source = 'documentcloud'
                             AND k.checked_at >= datetime('now', ?))
           ORDER BY c.closed_date DESC LIMIT ?""",
        (f"-{int(recheck_days)} days", int(max_queries)),
    ).fetchall()
    stamp = _now()
    queries = matches = 0
    state = "OK"
    for request in candidates:
        phrase = str(request["tracking_number"]).strip()
        url = DOCUMENTCLOUD_SEARCH + "?" + urlencode({"q": f'"{phrase}"', "per_page": 25})
        try:
            payload = fetch_json(url)
        except Exception as exc:  # blocked/outage: stop, record honestly
            state = f"BLOCKED:{type(exc).__name__}"
            break
        queries += 1
        hits = 0
        with conn:
            for doc in payload.get("results") or []:
                haystack = " ".join(str(doc.get(k) or "") for k in ("title", "description", "slug"))
                found = set(_tokens_for_text(haystack))
                # Full-text hits are accepted only when the exact phrase came back
                # in a highlight; title/description hits need the normalized token.
                highlights = json.dumps(doc.get("highlights") or {})
                if request["tracking_norm"] not in found and phrase.lower() not in highlights.lower():
                    continue
                ref = doc.get("canonical_url") or f"https://www.documentcloud.org/documents/{doc.get('id')}"
                _record_match(conn, request["request_key"], "documentcloud", ref,
                              "tracking_number_search", 0.75,
                              {"title": doc.get("title"), "organization": doc.get("organization"),
                               "source": doc.get("source")}, stamp)
                hits += 1
            conn.execute(
                """INSERT INTO closed_request_lookups(request_key,lookup_source,state,hits,checked_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(request_key,lookup_source) DO UPDATE SET
                   state=excluded.state, hits=excluded.hits, checked_at=excluded.checked_at""",
                (request["request_key"], "documentcloud", "OK", hits, stamp.replace("T", " ")[:19]),
            )
        matches += hits
    return {"candidates": len(candidates), "queries": queries, "matches": matches, "state": state}


def _period_label(closed: Optional[str], granularity: str) -> str:
    if not closed:
        return "undated"
    year, month = int(closed[:4]), int(closed[5:7])
    if granularity == "year":
        return f"{year}"
    if granularity == "quarter":
        return f"{year}-Q{(month - 1) // 3 + 1}"
    return f"{year}-{month:02d}"


def release_coverage(conn: sqlite3.Connection, *, granularity: str = "month", since: Optional[str] = None,
                     until: Optional[str] = None, agency: Optional[str] = None) -> Dict[str, Any]:
    """Released records vs records held, per agency and closing period."""
    if granularity not in ("month", "quarter", "year"):
        raise ValueError("granularity must be month, quarter or year")
    create_tables(conn)
    where, params = [], []
    if since:
        where.append("c.closed_date >= ?")
        params.append(since)
    if until:
        where.append("c.closed_date <= ?")
        params.append(until)
    if agency:
        where.append("c.agency_slug = ?")
        params.append(agency)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""SELECT c.agency_slug, c.closed_date, c.disposition, c.first_party_reason, c.releasable,
                   c.pages_released,
                   EXISTS(SELECT 1 FROM closed_request_matches m WHERE m.request_key=c.request_key
                          AND m.match_source='reading_room') AS held,
                   EXISTS(SELECT 1 FROM closed_request_matches m WHERE m.request_key=c.request_key
                          AND m.match_source IN ('muckrock','documentcloud')) AS elsewhere
            FROM closed_requests c {clause}""",
        params,
    ).fetchall()
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    totals = {"closed": 0, "first_party": 0, "released": 0, "not_released": 0, "held": 0,
              "elsewhere_only": 0, "gaps": 0, "pages_released": 0}
    for row in rows:
        key = (row["agency_slug"] or "unknown", _period_label(row["closed_date"], granularity))
        group = groups.setdefault(key, {k: 0 for k in totals})
        updates = {"closed": 1}
        if row["first_party_reason"]:
            updates["first_party"] = 1
        elif row["releasable"]:
            updates["released"] = 1
            updates["pages_released"] = row["pages_released"] or 0
            if row["held"]:
                updates["held"] = 1
            elif row["elsewhere"]:
                updates["elsewhere_only"] = 1
            else:
                updates["gaps"] = 1
        else:
            updates["not_released"] = 1
        for name, value in updates.items():
            group[name] += value
            totals[name] += value

    def share(group):
        return round(group["held"] / group["released"], 4) if group["released"] else None

    result_rows = [
        {"agency_slug": agency_slug, "period": period, **values, "held_share": share(values)}
        for (agency_slug, period), values in sorted(groups.items())
    ]
    return {"granularity": granularity, "since": since, "until": until, "agency": agency,
            "totals": {**totals, "held_share": share(totals)}, "rows": result_rows,
            "definitions": {
                "closed": "requests in agency logs with a closed date or terminal disposition",
                "first_party": "requests for the requester's own records; excluded from the denominator",
                "released": "full or partial grants that are not first-party (the denominator)",
                "held": "released requests with records in this archive (reading-room match)",
                "elsewhere_only": "released requests found only on MuckRock or DocumentCloud",
                "gaps": "released requests with no records located anywhere yet",
            }}


def release_gaps(conn: sqlite3.Connection, *, since: Optional[str] = None, until: Optional[str] = None,
                 agency: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Released third-party requests with no located records (follow-up candidates)."""
    create_tables(conn)
    where = ["c.releasable = 1", "c.tracking_number IS NOT NULL",
             "NOT EXISTS (SELECT 1 FROM closed_request_matches m WHERE m.request_key = c.request_key)"]
    params: List[Any] = []
    if since:
        where.append("c.closed_date >= ?")
        params.append(since)
    if until:
        where.append("c.closed_date <= ?")
        params.append(until)
    if agency:
        where.append("c.agency_slug = ?")
        params.append(agency)
    sql = (f"""SELECT c.*, a.name AS agency_name FROM closed_requests c
               LEFT JOIN agencies a ON a.slug = c.agency_slug
               WHERE {' AND '.join(where)} ORDER BY c.agency_slug, c.closed_date, c.tracking_number""")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    gaps = []
    for row in conn.execute(sql, params):
        gaps.append({
            "request_key": row["request_key"],
            "agency_slug": row["agency_slug"] or "unknown",
            "agency_name": row["agency_name"] or row["agency_slug"] or "unknown",
            "component": row["component"],
            "tracking_number": row["tracking_number"],
            "received_date": row["received_date"],
            "closed_date": row["closed_date"],
            "disposition": row["disposition"],
            "subject": row["subject"],
            "pages_released": row["pages_released"],
            "log_id": row["log_id"],
            "log_source_url": row["source_url"],
            "gap_reason": "no_matching_record",
        })
    return gaps


def write_gap_export(conn: sqlite3.Connection, out_dir, *, since: Optional[str] = None,
                     until: Optional[str] = None, agency: Optional[str] = None,
                     producer_commit: Optional[str] = None) -> Dict[str, Any]:
    """Write ``foia-release-gaps/1`` (manifest.json + gaps.jsonl) for Send_FOIA_Log_Requests."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gaps = release_gaps(conn, since=since, until=until, agency=agency)
    with (out / "gaps.jsonl").open("w", encoding="utf-8") as handle:
        for gap in gaps:
            handle.write(json.dumps(gap, sort_keys=True) + "\n")
    period_start = since or min((g["closed_date"] for g in gaps if g["closed_date"]), default=None)
    period_end = until or max((g["closed_date"] for g in gaps if g["closed_date"]), default=None)
    manifest = {
        "format": GAP_EXPORT_FORMAT,
        "generated_at": _now(),
        "producer": {"commit": producer_commit},
        "period": {"start": period_start or date.today().isoformat(),
                   "end": period_end or date.today().isoformat()},
        "count": len(gaps),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest

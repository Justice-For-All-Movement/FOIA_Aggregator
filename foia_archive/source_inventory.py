"""FC-01: universal federal source inventory with evidenced coverage counts.

Merges three inputs into one reproducible inventory keyed by a stable
``source_id`` derived from the normalized URL:

* FOIA.gov component census output (``build_component_census``) — official;
* the 18-element Intelligence Community registry — official;
* an explicit registry of recognized third-party public release archives.

Optionally joins the archive database for per-source discovered / archived /
indexed / remote-only counts, crawl access state (persistent 403 = BLOCKED,
never evaded) and FC-02 enumeration state. No network access happens here.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

from .intelligence_community_sources import normalized_ic_elements

INVENTORY_VERSION = "source-inventory/1"
PROVENANCE = ("official", "third_party")
ACCESS_STATES = ("ACTIVE", "BLOCKED", "FAILING", "UNVERIFIED", "HISTORICAL")
ENUMERATION_STATES = ("NOT_STARTED", "IN_PROGRESS", "EXHAUSTED", "BLOCKED", "FAILED")

# Recognized third-party public release archives. Entries are *candidates*:
# access stays UNVERIFIED until an adapter run records evidence, and records
# from them are attributed to the original agency and the publisher separately.
THIRD_PARTY_SOURCES: List[Dict[str, Any]] = [
    {
        "url": "https://www.muckrock.com/api_v1/foia/",
        "publisher": "MuckRock",
        "source_type": "third_party_request_archive",
        "enumeration_method": "json_api_next_link",
        "note": (
            "Public FOIA requests with agency communications and released "
            "files. A request page is not itself a released record; only "
            "attached response files are released documents (FC-04)."
        ),
    },
]


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if parts.port and not (scheme, parts.port) in (("https", 443), ("http", 80)):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def source_id_for(url: str) -> str:
    return "src_" + hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def _entry(url: str, provenance: str, **extra) -> Dict[str, Any]:
    normalized = normalize_url(url)
    return {
        "source_id": source_id_for(url),
        "url": normalized,
        "host": urlsplit(normalized).hostname,
        "provenance": provenance,
        "publisher": extra.get("publisher"),
        "source_types": set(),
        "attributions": [],
        "ic_elements": set(),
        "enumeration_method": extra.get("enumeration_method", "unknown"),
        "historical": False,
        "notes": [],
    }


def _merge(inventory: Dict[str, Dict[str, Any]], url: str, provenance: str, **extra):
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    sid = source_id_for(url)
    entry = inventory.get(sid)
    if entry is None:
        entry = inventory[sid] = _entry(url, provenance, **extra)
    elif provenance == "official":
        entry["provenance"] = "official"  # official evidence outranks a mirror
    if extra.get("source_type"):
        entry["source_types"].add(extra["source_type"])
    if extra.get("attribution"):
        attribution = extra["attribution"]
        if attribution not in entry["attributions"]:
            entry["attributions"].append(attribution)
    if extra.get("ic_element"):
        entry["ic_elements"].add(extra["ic_element"])
    if extra.get("historical"):
        entry["historical"] = True
    if extra.get("note") and extra["note"] not in entry["notes"]:
        entry["notes"].append(extra["note"])
    if extra.get("publisher") and not entry["publisher"]:
        entry["publisher"] = extra["publisher"]
    if extra.get("enumeration_method") and entry["enumeration_method"] == "unknown":
        entry["enumeration_method"] = extra["enumeration_method"]
    return entry


def _database_evidence(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    evidence: Dict[str, Dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT rr.url, rr.active, rr.last_successful_crawl_at, rr.last_error,
               rr.last_error_at,
               COUNT(DISTINCT d.id) AS discovered,
               COUNT(DISTINCT CASE WHEN d.download_status='downloaded' THEN d.id END) AS archived,
               COUNT(DISTINCT CASE WHEN t.extraction_status IN ('indexed','indexed_truncated')
                                   THEN d.id END) AS indexed
        FROM reading_rooms rr
        LEFT JOIN document_sources ds ON ds.reading_room_id = rr.id
        LEFT JOIN documents d ON d.id = ds.document_id
        LEFT JOIN document_text t ON t.document_id = d.id
        GROUP BY rr.id
        """
    ).fetchall()
    for row in rows:
        sid = source_id_for(row["url"])
        error = (row["last_error"] or "").lower()
        if "403" in error or "captcha" in error or "access blocked" in error or "access_blocked" in error:
            access = "BLOCKED"
        elif row["last_error"] and (row["last_error_at"] or "") > (row["last_successful_crawl_at"] or ""):
            access = "FAILING"
        elif row["last_successful_crawl_at"]:
            access = "ACTIVE"
        else:
            access = "UNVERIFIED"
        evidence[sid] = {
            "in_database": True,
            "active": bool(row["active"]),
            "access_status": access,
            "access_evidence_at": row["last_error_at"] if access in ("BLOCKED", "FAILING")
            else row["last_successful_crawl_at"],
            "discovered": row["discovered"],
            "archived": row["archived"],
            "indexed": row["indexed"],
            "remote_only": row["discovered"] - row["archived"],
        }
    has_cursors = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_cursors'"
    ).fetchone()
    if has_cursors:
        for row in conn.execute(
            """SELECT source_id, COUNT(*) AS windows,
                      SUM(state='EXHAUSTED') AS exhausted, SUM(state='BLOCKED') AS blocked,
                      SUM(state='FAILED') AS failed
               FROM source_cursors GROUP BY source_id"""
        ):
            if row["blocked"]:
                state = "BLOCKED"
            elif row["failed"]:
                state = "FAILED"
            elif row["exhausted"] == row["windows"]:
                state = "EXHAUSTED"
            else:
                state = "IN_PROGRESS"
            evidence.setdefault(row["source_id"], {})["enumeration_state"] = state
    return evidence


def build_source_inventory(
    census: Optional[Dict[str, Any]] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    ic_elements: Optional[Iterable[Dict[str, Any]]] = None,
    third_party_sources: Iterable[Dict[str, Any]] = THIRD_PARTY_SOURCES,
) -> Dict[str, Any]:
    inventory: Dict[str, Dict[str, Any]] = {}
    census = census or {}
    historical_components = {
        row.get("component_id") for row in census.get("components") or []
        if row.get("historical_note") and row.get("component_id") is not None
    }
    for instance in census.get("source_instances") or []:
        _merge(
            inventory, instance.get("url"), "official",
            source_type=instance.get("source_type") or instance.get("type"),
            historical=instance.get("component_id") in historical_components,
            attribution={k: instance.get(k) for k in ("agency_id", "agency_name", "component_id", "component_name")
                         if instance.get(k) is not None} or None,
        )
    elements = list(ic_elements) if ic_elements is not None else normalized_ic_elements()
    for element in elements:
        for source in element.get("sources") or []:
            _merge(inventory, source.get("url"), "official",
                   source_type=f"ic_{source.get('mode') or 'source'}",
                   ic_element=element.get("slug"), note=source.get("note"),
                   attribution={"ic_element": element.get("slug"),
                                "parent_agency": element.get("parent_agency")})
    for source in third_party_sources:
        _merge(inventory, source["url"], "third_party",
               publisher=source.get("publisher"), source_type=source.get("source_type"),
               enumeration_method=source.get("enumeration_method"), note=source.get("note"))

    evidence = _database_evidence(conn) if conn is not None else {}
    sources = []
    for sid in sorted(inventory):
        entry = inventory[sid]
        found = evidence.get(sid, {})
        entry["source_types"] = sorted(t for t in entry["source_types"] if t)
        entry["ic_elements"] = sorted(entry["ic_elements"])
        entry["in_database"] = bool(found.get("in_database"))
        entry["access_status"] = (
            "HISTORICAL" if entry["historical"] and not found.get("access_status")
            else found.get("access_status", "UNVERIFIED")
        )
        entry["access_evidence_at"] = found.get("access_evidence_at")
        entry["enumeration_state"] = found.get("enumeration_state", "NOT_STARTED")
        for key in ("discovered", "archived", "indexed", "remote_only"):
            entry[key] = found.get(key)  # None = not measured, never 0 by assumption
        sources.append(entry)
    # Database sources not explained by any inventory input are surfaced, not hidden.
    unexplained = sorted(sid for sid, item in evidence.items()
                         if item.get("in_database") and sid not in inventory)

    all_ic = {e.get("slug") for e in elements}
    covered_ic = {slug for s in sources for slug in s["ic_elements"]}

    def count(key, value):
        return sum(1 for s in sources if s[key] == value)

    measured = [s for s in sources if s["discovered"] is not None]
    return {
        "inventory_version": INVENTORY_VERSION,
        "summary": {
            "sources": len(sources),
            "official_sources": count("provenance", "official"),
            "third_party_sources": count("provenance", "third_party"),
            "access_status_counts": {state: count("access_status", state) for state in ACCESS_STATES},
            "enumeration_state_counts": {state: count("enumeration_state", state) for state in ENUMERATION_STATES},
            "ic_elements_expected": len(all_ic),
            "ic_elements_with_source": len(covered_ic & all_ic),
            "ic_elements_missing": sorted(all_ic - covered_ic),
            "sources_with_measured_counts": len(measured),
            "discovered": sum(s["discovered"] for s in measured),
            "archived": sum(s["archived"] for s in measured),
            "indexed": sum(s["indexed"] for s in measured),
            "remote_only": sum(s["remote_only"] for s in measured),
            "database_sources_not_in_inventory": len(unexplained),
        },
        "sources": sources,
        "database_sources_not_in_inventory": unexplained,
    }

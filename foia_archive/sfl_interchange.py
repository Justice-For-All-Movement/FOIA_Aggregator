"""Receive Send_FOIA_Log_Requests ``sfl-interchange/1`` bundles (SFL-12).

Two steps, so the web process never becomes an archive-database writer:

1. ``verify_envelope`` + ``stage_bundle`` (web endpoint, bearer-token
   authenticated): verify the bundle exactly as SFL's own ``import_bundle``
   does (manifest SHA-256 identity, per-file hashes and counts, artifact
   hashes/sizes) plus strict per-file field allowlists, then write it to an
   inbox directory. The acknowledgment is deterministic per bundle/status so
   retries are idempotent on both sides.
2. ``import_staged`` (authoritative writer, under the single-writer lock):
   re-verify from disk and load components/requests/fulfilled logs into
   ``sfl_*`` tables; tabular fulfilled logs also flow through FC-03.

Envelope (``foia-sfl-ingest/1``, JSON)::

    {"envelope_version": "foia-sfl-ingest/1",
     "manifest_json": "<exact manifest.json text>",
     "files": {"components.jsonl": "...", "requests.jsonl": "...", "fulfilled_logs.jsonl": "..."},
     "artifacts": {"<sha256>": "<base64 bytes>"}}
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .producer import producer_info
from .request_logs import RequestLogError, normalize_request_log, store_request_log

ENVELOPE_VERSION = "foia-sfl-ingest/1"
BUNDLE_VERSION = "sfl-interchange/1"
CONSUMER = "FOIA_Aggregator"
FILES = ("components.jsonl", "requests.jsonl", "fulfilled_logs.jsonl")
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
TABULAR_MEDIA = {
    "text/csv": "log.csv", "text/tab-separated-values": "log.tsv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "log.xlsx",
}
# Strict allowlists: anything else (e.g. requester data) rejects the bundle.
FIELDS = {
    "components.jsonl": {"component_id", "agency_id", "agency_name", "name", "status", "aliases", "routes"},
    "requests.jsonl": {"request_id", "component_id", "jurisdiction", "record_class", "period", "state",
                       "content_sha256"},
    "fulfilled_logs.jsonl": {"sha256", "filename", "media_type", "size", "request_id", "component_id",
                             "response_kind", "received_at", "coverage"},
}
ROUTE_FIELDS = {"kind", "destination", "state", "source_url"}
MANIFEST_FIELDS = {"schema_version", "generated_at", "counts", "request_states", "files", "excluded"}


class BundleRejected(ValueError):
    """The bundle is well-formed enough to acknowledge, but not acceptable."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _rows(text: str, name: str) -> List[Dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        raise BundleRejected(f"{name} is not valid JSON lines") from None
    for row in rows:
        if not isinstance(row, dict) or set(row) - FIELDS[name]:
            raise BundleRejected(f"{name} contains fields outside the allowlist")
        if name == "components.jsonl":
            for route in row.get("routes") or []:
                if not isinstance(route, dict) or set(route) - ROUTE_FIELDS:
                    raise BundleRejected("component route contains fields outside the allowlist")
    return rows


def verify_envelope(envelope: Any) -> Tuple[str, Dict[str, Any], Dict[str, List[dict]], Dict[str, bytes]]:
    """Return (bundle_sha256, manifest, rows by file, artifacts) or raise.

    ``ValueError`` (not ``BundleRejected``) means not even a bundle identity
    could be established, so no acknowledgment can be issued.
    """
    if not isinstance(envelope, dict) or envelope.get("envelope_version") != ENVELOPE_VERSION:
        raise ValueError("Unsupported envelope_version")
    raw_manifest = envelope.get("manifest_json")
    if not isinstance(raw_manifest, str) or not raw_manifest or len(raw_manifest) > 1024 * 1024:
        raise ValueError("manifest_json must be the exact manifest text")
    bundle_sha = _sha(raw_manifest.encode("utf-8"))
    try:
        manifest = json.loads(raw_manifest)
    except ValueError:
        raise BundleRejected("manifest_json is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BUNDLE_VERSION:
        raise BundleRejected("unsupported interchange version")
    if set(manifest) - MANIFEST_FIELDS:
        raise BundleRejected("manifest contains unexpected fields")
    files = envelope.get("files")
    if not isinstance(files, dict) or set(files) - set(FILES):
        raise BundleRejected("unexpected file in bundle")
    listed = manifest.get("files")
    if not isinstance(listed, list) or {e.get("path") for e in listed if isinstance(e, dict)} != set(FILES) \
            or len(listed) != len(FILES):
        raise BundleRejected("manifest must list exactly the interchange files")
    loaded: Dict[str, List[dict]] = {}
    for entry in listed:
        name = entry["path"]
        text = files.get(name)
        if not isinstance(text, str):
            raise BundleRejected(f"{name} missing from envelope")
        if _sha(text.encode("utf-8")) != entry.get("sha256"):
            raise BundleRejected(f"{name} SHA-256 mismatch")
        rows = _rows(text, name)
        if len(rows) != entry.get("records"):
            raise BundleRejected(f"{name} record count mismatch")
        loaded[name] = rows
    raw_artifacts = envelope.get("artifacts") or {}
    if not isinstance(raw_artifacts, dict):
        raise BundleRejected("artifacts must be an object")
    expected = {item.get("sha256") for item in loaded["fulfilled_logs.jsonl"]}
    if set(raw_artifacts) != expected or not all(_hex64(k) for k in expected):
        raise BundleRejected("artifacts do not match fulfilled_logs exactly")
    artifacts: Dict[str, bytes] = {}
    sizes = {item["sha256"]: item.get("size") for item in loaded["fulfilled_logs.jsonl"]}
    for sha, encoded in raw_artifacts.items():
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise BundleRejected("artifact is not valid base64") from None
        if len(data) > MAX_ARTIFACT_BYTES or _sha(data) != sha or len(data) != sizes[sha]:
            raise BundleRejected("artifact hash or size mismatch")
        artifacts[sha] = data
    return bundle_sha, manifest, loaded, artifacts


def acknowledgment(bundle_sha: str, status: str, detail: str, **extra) -> Dict[str, Any]:
    producer = producer_info()
    return {
        "ack_id": "ack_" + _sha(f"{bundle_sha}\x00{CONSUMER}\x00{status}".encode())[:24],
        "bundle_sha256": bundle_sha, "consumer": CONSUMER, "status": status, "detail": detail,
        "consumer_commit": producer["commit"], **extra,
    }


def stage_bundle(inbox: Path, bundle_sha: str, raw_manifest: str, files: Dict[str, str],
                 artifacts: Dict[str, bytes]) -> bool:
    """Atomically write the verified bundle to ``inbox/<bundle_sha>``. False if already staged."""
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / bundle_sha
    if target.exists():
        return False
    work = Path(tempfile.mkdtemp(prefix=".staging-", dir=inbox))
    try:
        (work / "manifest.json").write_text(raw_manifest, encoding="utf-8")
        for name in FILES:
            (work / name).write_text(files[name], encoding="utf-8")
        (work / "artifacts").mkdir()
        for sha, data in artifacts.items():
            (work / "artifacts" / sha).write_bytes(data)
        try:
            os.rename(work, target)
        except OSError:
            if target.exists():
                return False  # a concurrent identical delivery won
            raise
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
    return True


def receive(envelope: Any, inbox: Path, *, durable: bool) -> Tuple[int, Dict[str, Any]]:
    """Endpoint core: returns (HTTP status, acknowledgment or error body)."""
    try:
        bundle_sha, manifest, _, artifacts = verify_envelope(envelope)
    except BundleRejected as exc:
        bundle_sha = _sha(envelope["manifest_json"].encode("utf-8"))
        return 422, acknowledgment(bundle_sha, "rejected", str(exc), staged=False, duplicate=False)
    except ValueError as exc:
        return 400, {"detail": str(exc)}
    staged = stage_bundle(inbox, bundle_sha, envelope["manifest_json"], envelope["files"], artifacts)
    return (201 if staged else 200), acknowledgment(
        bundle_sha, "accepted",
        "verified and staged for the authoritative archive writer"
        + ("" if durable else "; inbox storage is ephemeral on this deployment"),
        staged=True, duplicate=not staged, durability="persistent" if durable else "ephemeral",
        counts=manifest.get("counts"),
    )


# -- authoritative import ---------------------------------------------------------------

def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sfl_bundles (
        bundle_sha256 TEXT PRIMARY KEY, schema_version TEXT NOT NULL, generated_at TEXT,
        counts_json TEXT NOT NULL, imported_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sfl_components (
        bundle_sha256 TEXT NOT NULL REFERENCES sfl_bundles(bundle_sha256), component_id TEXT NOT NULL,
        agency_id TEXT, agency_name TEXT, name TEXT, status TEXT, record_json TEXT NOT NULL,
        PRIMARY KEY (bundle_sha256, component_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sfl_requests (
        bundle_sha256 TEXT NOT NULL REFERENCES sfl_bundles(bundle_sha256), request_id TEXT NOT NULL,
        component_id TEXT, record_class TEXT, period_start TEXT, period_end TEXT, state TEXT,
        content_sha256 TEXT, PRIMARY KEY (bundle_sha256, request_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sfl_fulfilled_logs (
        bundle_sha256 TEXT NOT NULL REFERENCES sfl_bundles(bundle_sha256), artifact_sha256 TEXT NOT NULL,
        request_id TEXT NOT NULL, component_id TEXT, media_type TEXT, size INTEGER, response_kind TEXT,
        received_at TEXT, coverage_json TEXT NOT NULL, request_log_id TEXT, import_detail TEXT,
        PRIMARY KEY (bundle_sha256, artifact_sha256, request_id))""")


def _read_staged(directory: Path):
    raw_manifest = (directory / "manifest.json").read_text(encoding="utf-8")
    files = {name: (directory / name).read_text(encoding="utf-8") for name in FILES}
    artifacts = {p.name: base64.b64encode(p.read_bytes()).decode()
                 for p in (directory / "artifacts").iterdir()} if (directory / "artifacts").is_dir() else {}
    return {"envelope_version": ENVELOPE_VERSION, "manifest_json": raw_manifest, "files": files,
            "artifacts": artifacts}


def import_staged(conn: sqlite3.Connection, directory: Path) -> Dict[str, Any]:
    """Load one staged bundle. Caller holds the exclusive writer lock."""
    create_tables(conn)
    bundle_sha, manifest, loaded, artifacts = verify_envelope(_read_staged(Path(directory)))
    if Path(directory).name != bundle_sha:
        raise BundleRejected("staged directory name does not match bundle identity")
    if conn.execute("SELECT 1 FROM sfl_bundles WHERE bundle_sha256=?", (bundle_sha,)).fetchone():
        return {"bundle_sha256": bundle_sha, "imported": False, "request_logs": 0}
    components = {c["component_id"]: c for c in loaded["components.jsonl"]}
    normalized = []
    for item in loaded["fulfilled_logs.jsonl"]:
        log_id, detail = None, None
        filename = TABULAR_MEDIA.get(item.get("media_type"))
        if filename:
            agency = (components.get(item.get("component_id")) or {}).get("agency_id")
            try:
                log = normalize_request_log(artifacts[item["sha256"]],
                                            source_url=f"urn:sfl:{item['request_id']}:{item['sha256']}",
                                            filename=filename, agency=agency)
                normalized.append(log)
                log_id = log.log_id
            except RequestLogError as exc:
                detail = f"request log not normalized: {exc}"
        else:
            detail = "non-tabular artifact retained by reference only"
        item["_log_id"], item["_detail"] = log_id, detail
    stamp = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("INSERT INTO sfl_bundles VALUES (?,?,?,?,?)",
                     (bundle_sha, manifest["schema_version"], manifest.get("generated_at"),
                      json.dumps(manifest.get("counts"), sort_keys=True), stamp))
        for c in loaded["components.jsonl"]:
            conn.execute("INSERT INTO sfl_components VALUES (?,?,?,?,?,?,?)",
                         (bundle_sha, c["component_id"], c.get("agency_id"), c.get("agency_name"), c.get("name"),
                          c.get("status"), json.dumps(c, sort_keys=True)))
        for r in loaded["requests.jsonl"]:
            period = r.get("period") or [None, None]
            conn.execute("INSERT INTO sfl_requests VALUES (?,?,?,?,?,?,?,?)",
                         (bundle_sha, r["request_id"], r.get("component_id"), r.get("record_class"),
                          period[0], period[1], r.get("state"), r.get("content_sha256")))
        for f in loaded["fulfilled_logs.jsonl"]:
            conn.execute("INSERT INTO sfl_fulfilled_logs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (bundle_sha, f["sha256"], f["request_id"], f.get("component_id"), f.get("media_type"),
                          f.get("size"), f.get("response_kind"), f.get("received_at"),
                          json.dumps(f.get("coverage"), sort_keys=True), f["_log_id"], f["_detail"]))
    stored = sum(1 for log in normalized if store_request_log(conn, log))
    return {"bundle_sha256": bundle_sha, "imported": True, "request_logs": stored}


def envelope_from_directory(directory: Path) -> Dict[str, Any]:
    """Build an upload envelope from an SFL bundle directory (client/test helper)."""
    return _read_staged(Path(directory))

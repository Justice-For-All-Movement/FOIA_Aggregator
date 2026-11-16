"""Zero-storage catalog snapshots published as GitHub Release assets.

The scheduled crawl (the single authoritative writer) publishes a gzip'd,
integrity-checked SQLite snapshot (public record metadata, extracted text,
SHA-256 hashes; no secrets) to one release tag of this public repository,
keeping only the newest few. The web app downloads the newest snapshot at
startup when no object storage is configured.

Layout of the release ``catalog-snapshot``:
  foia-catalog-<UTC stamp>.db.gz   dated snapshots (newest N kept)
  latest.json                      {"asset", "sha256", "bytes", ...}
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .utils import logger

RELEASE_TAG = "catalog-snapshot"
MANIFEST_NAME = "latest.json"
ASSET_RE = re.compile(r"^foia-catalog-\d{8}T\d{6}Z\.db\.gz$")
DEFAULT_REPO = "Justice-For-All-Movement/FOIA_Aggregator"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# GitHub release assets may be up to 2 GiB; stay far below and keep restore
# within a free web instance's disk and memory.
MAX_COMPRESSED_BYTES = int(os.getenv("FOIA_SNAPSHOT_MAX_COMPRESSED_BYTES", str(900 * 1024 * 1024)))
MAX_RESTORED_BYTES = int(os.getenv("FOIA_SNAPSHOT_MAX_RESTORED_BYTES", str(4 * 1024 * 1024 * 1024)))


class SnapshotError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def database_counts(path: Path) -> dict:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        def scalar(sql):
            try:
                return int(conn.execute(sql).fetchone()[0] or 0)
            except sqlite3.Error:
                return 0
        return {
            "documents": scalar("SELECT COUNT(*) FROM documents"),
            "reading_rooms": scalar("SELECT COUNT(*) FROM reading_rooms"),
            "text_records": scalar("SELECT COUNT(*) FROM document_text WHERE character_count > 0"),
            "dated_documents": scalar(
                "SELECT COUNT(*) FROM documents WHERE published_date IS NOT NULL AND published_date != ''"),
            "newly_found": scalar("SELECT COUNT(*) FROM documents WHERE newly_found_at IS NOT NULL"),
            "originals_kept": scalar("SELECT COUNT(*) FROM documents WHERE download_status = 'downloaded'"),
            "text_only": scalar("SELECT COUNT(*) FROM documents WHERE download_status = 'text_only'"),
        }
    finally:
        conn.close()


def build_snapshot(db_path: Path, out_dir: Path, *, previous: Optional[dict] = None,
                   allow_shrink: bool = False, max_compressed_bytes: int = MAX_COMPRESSED_BYTES) -> dict:
    """Write ``foia-catalog-<stamp>.db.gz`` and ``latest.json`` into ``out_dir``.

    Refuses to publish a snapshot with fewer documents than ``previous``
    (never replace a larger verified catalog) or one over the size limit.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        raw = Path(tmp) / "snapshot.db"
        source = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        target = sqlite3.connect(str(raw))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        conn = sqlite3.connect(str(raw))
        try:
            # Small and self-contained: no WAL sidecar, no free pages.
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("VACUUM")
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise SnapshotError(f"snapshot integrity check failed: {check}")
        counts = database_counts(raw)
        if previous and not allow_shrink:
            before = int((previous.get("counts") or {}).get("documents") or 0)
            if counts["documents"] < before:
                raise SnapshotError(
                    f"refusing to publish a smaller catalog ({counts['documents']} < {before} documents)")
        raw_sha = hashlib.sha256()
        name = f"foia-catalog-{_stamp()}.db.gz"
        compressed = out_dir / name
        with raw.open("rb") as src, gzip.GzipFile(filename=str(compressed), mode="wb",
                                                    compresslevel=9, mtime=0) as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                raw_sha.update(chunk)
                dst.write(chunk)
        raw_bytes = raw.stat().st_size
    size = compressed.stat().st_size
    if size > max_compressed_bytes:
        compressed.unlink()
        raise SnapshotError(f"snapshot is {size} bytes compressed; limit {max_compressed_bytes}")
    manifest = {
        "schema": 1,
        "asset": name,
        "sha256": _file_sha256(compressed),
        "bytes": size,
        "sqlite_sha256": raw_sha.hexdigest(),
        "sqlite_bytes": raw_bytes,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": os.getenv("GITHUB_SHA") or os.getenv("FOIA_BUILD_COMMIT") or None,
        "counts": counts,
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_url(repo: str, name: str) -> str:
    if not REPO_RE.match(repo or ""):
        raise SnapshotError("invalid snapshot repository name")
    if name != MANIFEST_NAME and not ASSET_RE.match(name):
        raise SnapshotError("invalid snapshot asset name")
    return f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{name}"


def _get(url: str, timeout: int = 60, **kwargs):
    headers = {"User-Agent": "FOIAArchiveBot/0.1 (+https://github.com/Justice-For-All-Movement/FOIA_Aggregator)"}
    return requests.get(url, headers=headers, timeout=timeout, **kwargs)


def fetch_manifest(repo: str) -> Optional[dict]:
    response = _get(release_url(repo, MANIFEST_NAME), timeout=30)
    try:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        manifest = response.json()
    finally:
        response.close()
    if not isinstance(manifest, dict) or not ASSET_RE.match(str(manifest.get("asset") or "")):
        raise SnapshotError("snapshot manifest is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256") or "")):
        raise SnapshotError("snapshot manifest has no valid sha256")
    return manifest


def restore_latest(repo: str, destination: Path, *, overwrite: bool = False) -> Optional[dict]:
    """Download, verify (SHA-256, size, SQLite integrity) and install the
    newest snapshot. Returns the manifest, or None when none is published."""
    destination = Path(destination)
    if destination.exists() and destination.stat().st_size > 0 and not overwrite:
        raise SnapshotError("destination database already exists")
    manifest = fetch_manifest(repo)
    if manifest is None:
        return None
    if int(manifest.get("bytes") or 0) > MAX_COMPRESSED_BYTES:
        raise SnapshotError("published snapshot exceeds the restore size limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        compressed = Path(tmp) / "snapshot.db.gz"
        digest = hashlib.sha256()
        total = 0
        response = _get(release_url(repo, manifest["asset"]), timeout=120, stream=True)
        try:
            response.raise_for_status()
            with compressed.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_COMPRESSED_BYTES:
                        raise SnapshotError("snapshot download exceeds the size limit")
                    digest.update(chunk)
                    handle.write(chunk)
        finally:
            response.close()
        if digest.hexdigest() != manifest["sha256"]:
            raise SnapshotError("snapshot SHA-256 mismatch")
        raw = Path(tmp) / "snapshot.db"
        restored = 0
        with gzip.open(compressed, "rb") as src, raw.open("wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                restored += len(chunk)
                if restored > MAX_RESTORED_BYTES:
                    raise SnapshotError("snapshot expands beyond the restore size limit")
                dst.write(chunk)
        conn = sqlite3.connect(raw.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise SnapshotError("restored snapshot failed SQLite integrity check")
        os.replace(raw, destination)
    logger.info("Restored catalog snapshot %s (%s documents)", manifest["asset"],
                (manifest.get("counts") or {}).get("documents"))
    return manifest


def prune_plan(asset_names, keep: int) -> list[str]:
    """Dated snapshot assets to delete so that only the newest ``keep`` remain."""
    dated = sorted((n for n in asset_names if ASSET_RE.match(n)), reverse=True)
    return dated[max(1, int(keep)):]

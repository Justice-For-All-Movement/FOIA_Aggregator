"""FC-07: content-type completeness — signature sniffing, safe bundle
expansion, and distinct extraction outcomes by format.

* ``sniff`` identifies formats from magic bytes (not URLs or headers).
* ``safe_zip_members`` expands ZIP bundles under strict member-count,
  per-member, total-size, compression-ratio and nesting limits, rejecting
  traversal paths, symlinks and encrypted members (decompression bombs fail).
* ``format_coverage`` reports per format: indexed / empty / unsupported /
  failed / not attempted, so "indexed %" is never computed over unknowns.
"""
from __future__ import annotations

import io
import sqlite3
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Tuple

OUTCOMES = {
    "indexed": "INDEXED", "indexed_truncated": "INDEXED",
    "empty": "EMPTY",
    "unsupported": "UNSUPPORTED", "ocr_unavailable": "UNSUPPORTED",
    "extraction_failed": "FAILED", "ocr_failed": "FAILED",
}
MEDIA_METADATA_ONLY = {"audio/mpeg", "audio/wav", "video/mp4", "image/png", "image/jpeg", "image/gif"}


def sniff(data: bytes) -> str:
    head = data[:4096]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            return "application/zip"
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(n.startswith("xl/") for n in names):
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if any(n.startswith("ppt/") for n in names):
                return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return "application/zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"  # legacy .doc/.xls/.ppt
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"{\\rtf"):
        return "application/rtf"
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    lower = head.lstrip().lower()
    if lower.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    try:
        head.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


@dataclass(frozen=True)
class ZipLimits:
    max_members: int = 500
    max_member_bytes: int = 100 * 1024 * 1024
    max_total_bytes: int = 250 * 1024 * 1024
    max_ratio: float = 100.0
    max_depth: int = 1


class UnsafeBundle(ValueError):
    pass


def safe_zip_members(data: bytes, limits: ZipLimits = ZipLimits(), *, _depth: int = 0) -> List[Tuple[str, bytes]]:
    if _depth > limits.max_depth:
        raise UnsafeBundle("Nested archive depth exceeded")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UnsafeBundle("Not a readable ZIP") from None
    infos = [i for i in archive.infolist() if not i.is_dir()]
    if len(infos) > limits.max_members:
        raise UnsafeBundle("Too many members")
    declared = sum(i.file_size for i in infos)
    if declared > limits.max_total_bytes:
        raise UnsafeBundle("Declared expanded size exceeds limit")
    out, total = [], 0
    for info in infos:
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise UnsafeBundle("Member path escapes the bundle")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UnsafeBundle("Symlink members are not allowed")
        if info.flag_bits & 0x1:
            raise UnsafeBundle("Encrypted members are not supported")
        if info.file_size > limits.max_member_bytes:
            raise UnsafeBundle("Member exceeds size limit")
        if info.compress_size and info.file_size / info.compress_size > limits.max_ratio:
            raise UnsafeBundle("Compression ratio exceeds limit")
        with archive.open(info) as handle:  # bounded read: never trust declared sizes alone
            body = handle.read(info.file_size + 1)
        if len(body) > info.file_size:
            raise UnsafeBundle("Member expands beyond its declared size")
        total += len(body)
        if total > limits.max_total_bytes:
            raise UnsafeBundle("Expanded size exceeds limit")
        if sniff(body) == "application/zip":
            for name, inner in safe_zip_members(body, limits, _depth=_depth + 1):
                out.append((f"{path}/{name}", inner))
        else:
            out.append((str(path), body))
    return out


def extraction_outcome(status: str) -> str:
    return OUTCOMES.get(status or "", "FAILED" if status else "NOT_ATTEMPTED")


def format_coverage(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(lower(d.file_type),''),'unknown') AS fmt, t.extraction_status AS status,
                  COUNT(*) AS n
           FROM documents d LEFT JOIN document_text t ON t.document_id=d.id
           WHERE d.download_status='downloaded' GROUP BY 1, 2"""
    ).fetchall()
    table: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = table.setdefault(row["fmt"], dict.fromkeys(
            ("INDEXED", "EMPTY", "UNSUPPORTED", "FAILED", "NOT_ATTEMPTED"), 0))
        bucket[extraction_outcome(row["status"])] += row["n"]
    report = []
    for fmt in sorted(table):
        counts = table[fmt]
        attempted = sum(counts.values()) - counts["NOT_ATTEMPTED"]
        report.append({"format": fmt, "archived": sum(counts.values()), **{k.lower(): v for k, v in counts.items()},
                       "indexed_pct_of_attempted": round(100 * counts["INDEXED"] / attempted, 1) if attempted else None})
    return report

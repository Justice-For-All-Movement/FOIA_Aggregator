"""FC-08: cross-source identity and dedupe provenance (relations, never merges).

Detects, and records as evidence in ``document_relations``:

* ``url_variant`` — different stored URLs that normalize to the same URL;
* ``identical_bytes`` — different records whose archived binaries share SHA-256;
* ``text_near_duplicate`` — different bytes, highly similar extracted text
  (a re-release, a different redaction, a reformatted copy);
* ``redaction_variant`` — a near-duplicate pair whose redaction markers differ;
* ``third_party_copy`` — a third-party release crosslinked to an official record.

Nothing is deleted, merged or rewritten: every source association and content
hash stays on its own record. ``identity_group`` (union of url_variant and
identical_bytes only) gives the "same object" grouping; near-duplicates and
redaction variants are *related public versions* that stay separate.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .source_inventory import normalize_url

RELATIONS = ("url_variant", "identical_bytes", "text_near_duplicate", "redaction_variant", "third_party_copy")
SHINGLE = 5
NUM_HASHES = 64
BANDS = 16
NEAR_DUPLICATE_THRESHOLD = 0.8
MIN_WORDS = 20
REDACTION_PATTERN = re.compile(
    r"\(b\)\s*\(\d\)(?:\s*\([A-Za-z]\))?|\bb\s?\(?\d\)?(?:\s*\([A-Za-z]\))?\b|\[?redacted\]?|█+",
    re.IGNORECASE,
)


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS document_relations (
            doc_a INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            doc_b INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            relation TEXT NOT NULL CHECK(relation IN
                ('url_variant','identical_bytes','text_near_duplicate','redaction_variant','third_party_copy')),
            score REAL,
            evidence_json TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            CHECK (doc_a < doc_b),
            PRIMARY KEY (doc_a, doc_b, relation)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_relations_b ON document_relations(doc_b)")


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def shingles(text: str, size: int = SHINGLE) -> Set[int]:
    words = _words(text)
    if len(words) < size:
        return {int(hashlib.blake2b(" ".join(words).encode(), digest_size=8).hexdigest(), 16)} if words else set()
    return {int(hashlib.blake2b(" ".join(words[i:i + size]).encode(), digest_size=8).hexdigest(), 16)
            for i in range(len(words) - size + 1)}


_MASK = (1 << 61) - 1
_SEEDS = [(int(hashlib.sha256(f"a{i}".encode()).hexdigest(), 16) % _MASK or 1,
           int(hashlib.sha256(f"b{i}".encode()).hexdigest(), 16) % _MASK) for i in range(NUM_HASHES)]


def minhash(shingle_set: Set[int]) -> Tuple[int, ...]:
    return tuple(min(((a * s + b) % _MASK) for s in shingle_set) for a, b in _SEEDS)


def jaccard(a: Set[int], b: Set[int]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def redaction_markers(text: str) -> int:
    return len(REDACTION_PATTERN.findall(text))


def _pair(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _record(conn, a, b, relation, score, evidence, stamp) -> int:
    a, b = _pair(a, b)
    if a == b:
        return 0
    return conn.execute(
        """INSERT INTO document_relations(doc_a,doc_b,relation,score,evidence_json,detected_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(doc_a,doc_b,relation) DO UPDATE SET
           score=excluded.score, evidence_json=excluded.evidence_json""",
        (a, b, relation, score, json.dumps(evidence, sort_keys=True), stamp),
    ).rowcount


def analyze_identity(conn: sqlite3.Connection, *, threshold: float = NEAR_DUPLICATE_THRESHOLD,
                     max_documents: int = 200_000) -> Dict[str, int]:
    """Recompute relation evidence. Caller holds the single-writer lock."""
    if not 0.5 <= threshold <= 1.0:
        raise ValueError("threshold must be within 0.5..1.0")
    create_tables(conn)
    stamp = datetime.now(timezone.utc).isoformat()
    counts = dict.fromkeys(RELATIONS, 0)
    with conn:
        by_url: Dict[str, List[int]] = defaultdict(list)
        by_hash: Dict[str, List[int]] = defaultdict(list)
        for row in conn.execute(
            "SELECT id, url, sha256, download_status FROM documents WHERE url IS NOT NULL ORDER BY id LIMIT ?",
            (max_documents,),
        ):
            by_url[normalize_url(row["url"])].append(row["id"])
            if row["download_status"] == "downloaded" and row["sha256"]:
                by_hash[row["sha256"]].append(row["id"])
        for url, ids in by_url.items():
            for other in ids[1:]:
                counts["url_variant"] += _record(conn, ids[0], other, "url_variant", 1.0,
                                                 {"normalized_url": url}, stamp)
        for sha, ids in by_hash.items():
            for other in ids[1:]:
                counts["identical_bytes"] += _record(conn, ids[0], other, "identical_bytes", 1.0,
                                                     {"sha256": sha}, stamp)

        texts = {}
        sigs = {}
        for row in conn.execute(
            """SELECT t.document_id, t.body, d.sha256 FROM document_text t JOIN documents d ON d.id=t.document_id
               WHERE t.extraction_status IN ('indexed','indexed_truncated') ORDER BY t.document_id LIMIT ?""",
            (max_documents,),
        ):
            body = row["body"] or ""
            if len(_words(body)) < MIN_WORDS:
                continue  # too short to tell versions apart from boilerplate
            sh = shingles(body)
            texts[row["document_id"]] = (sh, row["sha256"], redaction_markers(body))
            sigs[row["document_id"]] = minhash(sh)
        rows_per_band = NUM_HASHES // BANDS
        candidates: Set[Tuple[int, int]] = set()
        for band in range(BANDS):
            buckets: Dict[Tuple[int, ...], List[int]] = defaultdict(list)
            for doc_id, sig in sigs.items():
                buckets[sig[band * rows_per_band:(band + 1) * rows_per_band]].append(doc_id)
            for ids in buckets.values():
                if 1 < len(ids) <= 500:  # oversized buckets are boilerplate, not evidence
                    for i, a in enumerate(ids):
                        for b in ids[i + 1:]:
                            candidates.add(_pair(a, b))
        for a, b in sorted(candidates):
            (sa, ha, ra), (sb, hb, rb) = texts[a], texts[b]
            if ha and hb and ha == hb:
                continue  # already identical bytes
            score = jaccard(sa, sb)
            if score < threshold:
                continue
            evidence = {"jaccard": round(score, 4), "shingle_size": SHINGLE,
                        "redaction_markers": [ra, rb]}
            counts["text_near_duplicate"] += _record(conn, a, b, "text_near_duplicate", score, evidence, stamp)
            if ra != rb:
                counts["redaction_variant"] += _record(conn, a, b, "redaction_variant", score, evidence, stamp)

        has_tp = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='third_party_releases'").fetchone()
        if has_tp:
            for row in conn.execute(
                """SELECT t.official_document_id AS official, d.id AS copy, t.release_id, t.publisher
                   FROM third_party_releases t JOIN documents d ON d.url = t.file_url
                   WHERE t.official_document_id IS NOT NULL AND d.id != t.official_document_id"""
            ):
                counts["third_party_copy"] += _record(
                    conn, row["official"], row["copy"], "third_party_copy", 1.0,
                    {"release_id": row["release_id"], "publisher": row["publisher"]}, stamp)
    return counts


def identity_groups(conn: sqlite3.Connection) -> Dict[int, int]:
    """doc id -> group root, joining only url_variant and identical_bytes."""
    create_tables(conn)
    parent: Dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in conn.execute(
        "SELECT doc_a, doc_b FROM document_relations WHERE relation IN ('url_variant','identical_bytes')"
    ):
        ra, rb = find(row[0]), find(row[1])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {doc: find(doc) for doc in list(parent)}


def related_versions(conn: sqlite3.Connection, document_id: int) -> List[Dict[str, object]]:
    """Related public records for display; each stays a separate record."""
    rows = conn.execute(
        """SELECT CASE WHEN r.doc_a=? THEN r.doc_b ELSE r.doc_a END AS other, r.relation, r.score,
                  r.evidence_json, d.title, d.url, d.download_status
           FROM document_relations r
           JOIN documents d ON d.id = CASE WHEN r.doc_a=? THEN r.doc_b ELSE r.doc_a END
           WHERE r.doc_a=? OR r.doc_b=? ORDER BY r.relation, other""",
        (document_id, document_id, document_id, document_id),
    ).fetchall()
    return [{"document_id": r["other"], "relation": r["relation"], "score": r["score"],
             "evidence": json.loads(r["evidence_json"]), "title": r["title"], "url": r["url"],
             "archived": r["download_status"] == "downloaded"} for r in rows]


def relation_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    create_tables(conn)
    out = dict.fromkeys(RELATIONS, 0)
    for row in conn.execute("SELECT relation, COUNT(*) FROM document_relations GROUP BY relation"):
        out[row[0]] = row[1]
    groups = identity_groups(conn)
    out["documents_in_identity_groups"] = len(groups)
    out["identity_groups"] = len(set(groups.values()))
    return out

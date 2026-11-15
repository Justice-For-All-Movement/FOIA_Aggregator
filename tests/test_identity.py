"""FC-08: cross-source identity relations (fixtures; nothing merged or deleted)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.export_extensions import export_relations_page, load_extension_schema
from foia_archive.identity import (
    analyze_identity, identity_groups, jaccard, redaction_markers, related_versions,
    relation_summary, shingles,
)
from foia_archive.muckrock_adapter import create_tables as create_tp_tables
from foia_archive.storage import MIGRATIONS, get_connection, init_db
from tests.test_export_contract import asgi_request, validate
from ui import server

BASE = " ".join(
    f"Paragraph {i}: the Department reviewed contract file {i * 7} for the building maintenance program "
    f"and found that invoice batch {i * 13} for fiscal year {2000 + i} was approved by contracting officer "
    f"number {i * 3} after review of statement of work section {i}."
    for i in range(1, 16)
)
OTHER = " ".join(
    f"Chapter {i}: training manual page {i * 11} describes onboarding step {i}, the security briefing "
    f"schedule for cohort {i * 5}, and equipment request form {i * 17} for the logistics office."
    for i in range(1, 16)
)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "a.db"
        init_db(self.db, Path(self.temp.name) / "files")
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)
        docs = [
            # id, url, sha, text
            (1, "https://foia.example.gov/a.pdf", "1" * 64, BASE),
            (2, "https://FOIA.example.gov/a.pdf#page=1", None, None),           # URL variant of 1
            (3, "https://mirror.example.gov/copy-of-a.pdf", "1" * 64, BASE),  # identical bytes
            (4, "https://foia.example.gov/a-redacted.pdf", "2" * 64,
             BASE + " The name of the reviewing officer was withheld under (b)(6) and (b)(7)(C)."),
            (5, "https://foia.example.gov/manual.pdf", "3" * 64, OTHER),
        ]
        for doc_id, url, sha, text in docs:
            self.conn.execute("INSERT INTO documents(id,url,sha256,download_status,title) VALUES(?,?,?,?,?)",
                              (doc_id, url, sha, "downloaded" if sha else "pending", f"Doc {doc_id}"))
            if text:
                self.conn.execute("INSERT INTO document_text(document_id,body,extraction_status,extraction_method,"
                                  "character_count,truncated) VALUES(?,?,'indexed','native_text',?,0)",
                                  (doc_id, text, len(text)))
        self.conn.commit()

    def test_migration_12(self):
        self.assertIn(12, [v for v, _, _ in MIGRATIONS])

    def test_relations_detected_without_merging(self):
        counts = analyze_identity(self.conn)
        self.assertEqual(counts["url_variant"], 1)
        self.assertEqual(counts["identical_bytes"], 1)
        relations = {(r["doc_a"], r["doc_b"], r["relation"])
                     for r in self.conn.execute("SELECT * FROM document_relations")}
        self.assertIn((1, 2, "url_variant"), relations)
        self.assertIn((1, 3, "identical_bytes"), relations)
        self.assertIn((1, 4, "redaction_variant"), relations)
        self.assertIn((1, 4, "text_near_duplicate"), relations)
        self.assertNotIn((1, 3, "text_near_duplicate"), relations)  # identical bytes is not a "version"
        self.assertFalse(any(5 in (a, b) for a, b, _ in relations))  # unrelated record untouched
        # Nothing merged or deleted.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
        groups = identity_groups(self.conn)
        self.assertEqual(groups[1], groups[2])
        self.assertEqual(groups[1], groups[3])
        self.assertNotIn(4, groups)  # redaction variant stays a separate object
        summary = relation_summary(self.conn)
        self.assertEqual((summary["identity_groups"], summary["documents_in_identity_groups"]), (1, 3))

    def test_rerun_is_idempotent(self):
        analyze_identity(self.conn)
        before = self.conn.execute("SELECT COUNT(*) FROM document_relations").fetchone()[0]
        analyze_identity(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM document_relations").fetchone()[0], before)

    def test_third_party_copy_crosslink(self):
        create_tp_tables(self.conn)
        self.conn.execute("INSERT INTO documents(id,url,download_status) VALUES(6,'https://cdn.muckrock.com/x.pdf','pending')")
        self.conn.execute("""INSERT INTO third_party_releases VALUES('tpr_x','MuckRock','src','1','https://www.muckrock.com/foi/1/',
                             NULL,NULL,NULL,NULL,'https://cdn.muckrock.com/x.pdf',NULL,NULL,0,1,'2026-01-01')""")
        self.conn.commit()
        self.assertEqual(analyze_identity(self.conn)["third_party_copy"], 1)

    def test_helpers(self):
        self.assertEqual(jaccard(shingles(BASE), shingles(BASE)), 1.0)
        self.assertLess(jaccard(shingles(BASE), shingles(OTHER)), 0.1)
        self.assertEqual(redaction_markers("withheld (b)(6) and (b)(7)(C) [REDACTED] ███"), 4)
        with self.assertRaises(ValueError):
            analyze_identity(self.conn, threshold=0.1)

    def test_related_versions_and_relations_export(self):
        analyze_identity(self.conn)
        related = {r["document_id"]: r["relation"] for r in related_versions(self.conn, 4)}
        self.assertIn(related[1], ("text_near_duplicate", "redaction_variant"))
        page = export_relations_page(self.conn)
        schema = load_extension_schema("document-relation.v1")
        for record in page["records"]:
            validate(record, schema)
            self.assertEqual(record["record_ids"], sorted(record["record_ids"]))
        self.assertEqual(len({r["relation_id"] for r in page["records"]}), len(page["records"]))

    def test_record_page_shows_related_versions_separately(self):
        analyze_identity(self.conn)
        with patch("ui.server.get_db", side_effect=lambda: get_connection(self.db)):
            status, _, body = asyncio.run(asgi_request(server.app, "/record/4"))
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn("Related public versions", html)
        self.assertIn("Different redactions", html)
        self.assertIn('href="/record/1"', html)


if __name__ == "__main__":
    unittest.main()

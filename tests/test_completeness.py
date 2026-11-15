"""FC-12 completeness denominators and FC-14 corpus review (fixtures)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.completeness import UNKNOWABLE, build_corpus_review, compute_completeness
from foia_archive.identity import analyze_identity
from foia_archive.storage import get_connection, init_db
from tests.test_export_contract import asgi_request, build_fixture_db
from ui import server


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = build_fixture_db(Path(self.temp.name))
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)

    def test_distinct_denominators_and_explicit_unknowns(self):
        c = compute_completeness(self.conn)
        self.assertEqual({k: c["records"][k] for k in ("cataloged", "archived", "metadata_only", "archived_bytes",
                                                       "text_indexed", "ocr_indexed")},
                         {"cataloged": 3, "archived": 1, "metadata_only": 2, "archived_bytes": 1234,
                          "text_indexed": 1, "ocr_indexed": 0})
        self.assertIsNone(c["unknowns"]["total_public_foia_records"])
        self.assertEqual(c["unknowns"]["note"], UNKNOWABLE)
        self.assertEqual(c["unknowns"]["records_without_published_date"], 1)
        self.assertEqual(c["intelligence_community"], {"expected": 18, "with_source": 18, "missing": []})
        self.assertEqual(c["sources"]["database_sources"], 2)
        self.assertEqual({r["year"] for r in c["by_year"]}, {"2019", "2021", "unknown"})
        self.assertEqual(c["by_agency"][0]["agency"], "Example Department")

    def test_empty_database_reports_zeros_not_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            init_db(Path(temp) / "e.db", Path(temp) / "f")
            conn = get_connection(Path(temp) / "e.db")
            try:
                c = compute_completeness(conn)
                review = build_corpus_review(conn)
            finally:
                conn.close()
        self.assertEqual(c["records"]["cataloged"], 0)
        self.assertFalse(review["all_gates_pass"])

    def test_corpus_review_is_draft_and_never_unblocks(self):
        analyze_identity(self.conn)
        review = build_corpus_review(self.conn)
        self.assertEqual((review["review_status"], review["downstream_unblocked"]), ("draft", False))
        gates = {g["id"]: g["result"] for g in review["gates"]}
        self.assertEqual(gates["ic_coverage"], "pass")
        self.assertEqual(gates["archive_hashes"], "pass")
        self.assertEqual(gates["multi_agency"], "fail")
        self.assertIn(gates["recovery_evidence"], ("unknown", "fail"))
        self.assertFalse(review["all_gates_pass"])
        self.assertEqual(review["missing_known_files"]["metadata_only"], 2)
        self.assertIn("pdf", {r["file_type"] for r in review["strata"]["document_type"]})

    def test_archive_hash_gate_fails_when_hash_missing(self):
        self.conn.execute("UPDATE documents SET sha256=NULL WHERE download_status='downloaded'")
        self.conn.commit()
        gates = {g["id"]: g["result"] for g in build_corpus_review(self.conn)["gates"]}
        self.assertEqual(gates["archive_hashes"], "fail")

    def test_coverage_page_and_api(self):
        server._COVERAGE_CACHE.update(updated=0.0, data=None)
        server._EXPORT_RATE.clear()
        with patch("ui.server.get_db", side_effect=lambda: get_connection(self.db)):
            status, _, body = asyncio.run(asgi_request(server.app, "/coverage"))
            self.assertEqual(status, 200)
            self.assertIn("not knowable", body.decode())
            self.assertIn("Example Department", body.decode())
            status, _, body = asyncio.run(asgi_request(server.app, "/api/v1/coverage"))
        data = json.loads(body)
        self.assertEqual((status, data["records"]["cataloged"]), (200, 3))
        self.assertIn("archive_connected", data["archive"])


if __name__ == "__main__":
    unittest.main()

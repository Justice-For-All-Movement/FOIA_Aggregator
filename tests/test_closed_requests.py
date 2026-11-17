"""Closed-request master list, release matching, coverage and gap export."""
import json
import tempfile
import unittest
from pathlib import Path

from foia_archive.closed_requests import (
    GAP_EXPORT_FORMAT, build_master_list, first_party_reason, match_documentcloud,
    match_muckrock, match_reading_room, normalize_disposition, normalize_tracking,
    release_coverage, release_gaps, write_gap_export,
)
from foia_archive.request_logs import normalize_request_log, store_request_log
from foia_archive.storage import get_connection, init_db

FIXTURE = Path(__file__).parent / "fixtures" / "published_logs" / "closed_log_q3.csv"
AGENCY = "example-department"


class NormalizationTests(unittest.TestCase):
    def test_dispositions(self):
        cases = {
            "Granted in Full": "full_grant", "Full Grant": "full_grant",
            "Partial Grant/Partial Denial": "partial_grant", "Released in part": "partial_grant",
            "Full Denial Based on Exemptions": "full_denial", "No Records": "no_records",
            "Request Withdrawn": "withdrawn", "Referred to another agency": "referred",
            "Improper FOIA Request for Other Reasons": "improper", "": "unknown", "In Process": "other",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_disposition(raw), expected, raw)

    def test_first_party(self):
        self.assertEqual(first_party_reason("Copy of my A-file"), "a_file")
        self.assertEqual(first_party_reason("DD-214 for my father"), "personnel_record")
        self.assertEqual(first_party_reason("Contracts", {"Request Type": "First Party"}), "logged_first_party")
        self.assertIsNone(first_party_reason("Records about myself", {"Request Type": "Third Party"}))
        self.assertIsNone(first_party_reason("Surveillance contracts with vendors"))

    def test_tracking_specificity(self):
        self.assertEqual(normalize_tracking("ex-2026 000101"), "EX2026000101")
        self.assertIsNone(normalize_tracking("FOIA"))
        self.assertIsNone(normalize_tracking("12-3"))


class MasterListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "t.db"
        init_db(db, Path(self.tmp.name) / "files")
        self.conn = get_connection(db)
        log = normalize_request_log(FIXTURE.read_bytes(), source_url="https://foia.example.gov/q3.csv",
                                    agency=AGENCY)
        self.assertEqual(log.rows[0]["pages_released"], "1,204")
        store_request_log(self.conn, log)
        # Same request in a second log (later re-publication) must not double count.
        dup = FIXTURE.read_bytes().replace(b"Synthetic", b"Republished synthetic")
        store_request_log(self.conn, normalize_request_log(dup, source_url="https://foia.example.gov/q3b.csv",
                                                           agency=AGENCY))
        self.conn.execute("INSERT INTO agencies(slug,name) VALUES(?,?)", (AGENCY, "Example Department"))
        agency_id = self.conn.execute("SELECT id FROM agencies WHERE slug=?", (AGENCY,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO documents(url,filename,title,agency_id) VALUES(?,?,?,?)",
            ("https://foia.example.gov/rr/EX-2026-000101_release.pdf", "EX-2026-000101_release.pdf",
             "Interim release", agency_id))
        self.conn.commit()
        self.summary = build_master_list(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_master_list_counts(self):
        self.assertEqual(self.summary["closed_requests"], 7)  # open request excluded, duplicates merged
        self.assertEqual(self.summary["first_party"], 2)
        self.assertEqual(self.summary["releasable"], 3)  # 101, 102, 105
        row = self.conn.execute("SELECT * FROM closed_requests WHERE tracking_number='EX-2026-000101'").fetchone()
        self.assertEqual(row["pages_released"], 1204)
        self.assertEqual(row["disposition"], "full_grant")

    def test_matching_coverage_and_gaps(self):
        rr = match_reading_room(self.conn)
        self.assertEqual(rr["metadata_matches"], 1)
        self.conn.execute(
            """INSERT INTO third_party_requests(publisher,request_id,title,status,agency_name,tracking_id,
                   public_url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("MuckRock", "77", "Surveillance procurement", "done", "Example Department",
             "EX-2026-000102", "https://www.muckrock.com/foi/x-77/", "t", "t"))
        self.conn.commit()
        self.assertEqual(match_muckrock(self.conn)["matches"], 1)

        calls = []

        def fake(url):
            calls.append(url)
            return {"results": [{"id": 9, "title": "Body camera memo EX-2026-000105",
                                 "canonical_url": "https://www.documentcloud.org/documents/9-x"}]}

        dc = match_documentcloud(self.conn, fake, max_queries=5)
        self.assertEqual((dc["queries"], dc["matches"]), (1, 1))
        self.assertEqual(match_documentcloud(self.conn, fake)["queries"], 0)  # nothing left to check

        report = release_coverage(self.conn, granularity="quarter")
        self.assertEqual(report["totals"]["released"], 3)
        self.assertEqual(report["totals"]["held"], 1)
        self.assertEqual(report["totals"]["elsewhere_only"], 2)
        self.assertEqual(report["totals"]["gaps"], 0)
        self.assertEqual(report["totals"]["pages_released"], 1204 + 88 + 12)
        q3 = next(r for r in report["rows"] if r["period"] == "2026-Q3")
        self.assertEqual(q3["held_share"], round(1 / 3, 4))

    def test_gap_export(self):
        gaps = release_gaps(self.conn)
        self.assertEqual({g["tracking_number"] for g in gaps},
                         {"EX-2026-000101", "EX-2026-000102", "EX-2026-000105"})
        match_reading_room(self.conn)
        with tempfile.TemporaryDirectory() as out:
            manifest = write_gap_export(self.conn, out, producer_commit=None)
            self.assertEqual(manifest["format"], GAP_EXPORT_FORMAT)
            self.assertEqual(manifest["count"], 2)
            lines = [json.loads(l) for l in Path(out, "gaps.jsonl").read_text().splitlines()]
            self.assertEqual(lines[0]["agency_name"], "Example Department")
            self.assertTrue(all(l["disposition"] in ("full_grant", "partial_grant") for l in lines))
            self.assertNotIn("requester_name", lines[0])

    def test_rebuild_is_idempotent_and_drops_stale_matches(self):
        match_reading_room(self.conn)
        again = build_master_list(self.conn)
        self.assertEqual(again["closed_requests"], 7)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM closed_request_matches").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()


class ReleaseCoverageWebTests(unittest.TestCase):
    def test_page_api_and_gap_export(self):
        import asyncio
        from unittest.mock import patch
        from tests.test_presentation_ui import asgi_get
        from ui import server

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "w.db"
        init_db(db, Path(tmp.name) / "files")
        conn = get_connection(db)
        store_request_log(conn, normalize_request_log(FIXTURE.read_bytes(),
                                                      source_url="https://foia.example.gov/q3.csv", agency=AGENCY))
        build_master_list(conn)
        conn.close()
        with patch("ui.server.get_db", side_effect=lambda: get_connection(db)):
            status, body = asyncio.run(asgi_get(server.app, "/release-coverage"))
            self.assertEqual(status, 200)
            self.assertIn("Released (denominator)", body)
            status, body = asyncio.run(asgi_get(server.app, "/api/v1/release-coverage",
                                                params={"granularity": "quarter"}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["totals"]["released"], 3)
            status, body = asyncio.run(asgi_get(server.app, "/api/v1/export/release-gaps.jsonl"))
            self.assertEqual(status, 200)
            self.assertEqual(len(body.splitlines()), 3)
            status, _ = asyncio.run(asgi_get(server.app, "/api/v1/release-coverage",
                                             params={"since": "bad"}))
            self.assertEqual(status, 400)

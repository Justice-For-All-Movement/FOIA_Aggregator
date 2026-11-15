"""FC-04: MuckRock adapter against recorded-shape fixtures (no network)."""
import json
import tempfile
import unittest
from pathlib import Path

from foia_archive.enumeration import coverage_report
from foia_archive.muckrock_adapter import API_ROOT, ingest, parse_page, parse_request
from foia_archive.scraper_core import SourceAccessBlocked
from foia_archive.source_inventory import build_source_inventory, source_id_for
from foia_archive.storage import MIGRATIONS, get_connection, init_db

FIXTURES = Path(__file__).parent / "fixtures" / "muckrock"
START = API_ROOT + "?format=json&status=done"
PAGES = {
    START: json.loads((FIXTURES / "foia_page_1.json").read_text()),
    "https://www.muckrock.com/api_v1/foia/?format=json&page=2&status=done":
        json.loads((FIXTURES / "foia_page_2.json").read_text()),
}


class Recorded:
    def __init__(self, pages=PAGES, block=False):
        self.pages, self.block, self.calls = pages, block, []

    def __call__(self, url):
        self.calls.append(url)
        if self.block:
            raise SourceAccessBlocked("HTTP 403")
        return self.pages[url]


class ParseTests(unittest.TestCase):
    def test_only_agency_response_files_are_releases(self):
        parsed = parse_request(PAGES[START]["results"][0], {"77": "Example Department"})
        request = parsed["request"]
        self.assertEqual(request["request_url"],
                         "https://www.muckrock.com/foi/example-dept-77/fixture-building-maintenance-contracts-1001/")
        self.assertEqual((request["agency_ref"], request["agency_name"]), ("77", "Example Department"))
        # Requester upload skipped; agency response files kept, with publisher attribution.
        self.assertEqual(parsed["skipped"]["non_response_files"], 1)
        urls = [r.file_url for r in parsed["releases"]]
        self.assertEqual(urls, ["https://cdn.muckrock.com/foia_files/2023/11/15/contracts.pdf"])
        # A link to an external host is not trusted as a MuckRock-hosted release.
        self.assertEqual(parsed["skipped"]["unsafe_file_urls"], 1)
        self.assertEqual(parsed["releases"][0].publisher, "MuckRock")

    def test_request_without_responsive_files_yields_no_document(self):
        parsed = parse_request(PAGES[START]["results"][1])
        self.assertEqual(parsed["releases"], [])
        self.assertEqual(parsed["request"]["agency_name"], "Example Bureau")

    def test_unsafe_file_urls_rejected(self):
        page2 = PAGES["https://www.muckrock.com/api_v1/foia/?format=json&page=2&status=done"]
        parsed = parse_request(page2["results"][0])
        self.assertEqual([r.file_url for r in parsed["releases"]],
                         ["https://s3.amazonaws.com/muckrock/foia_files/manual.pdf"])
        self.assertEqual(parsed["skipped"]["unsafe_file_urls"], 3)

    def test_release_ids_are_stable(self):
        a = parse_request(PAGES[START]["results"][0])["releases"][0].release_id
        b = parse_request(PAGES[START]["results"][0])["releases"][0].release_id
        self.assertEqual(a, b)

    def test_foreign_next_link_rejected(self):
        from foia_archive.enumeration import PaginationError
        payload = dict(PAGES[START], next="https://evil.example/api?page=2")
        with self.assertRaises(PaginationError):
            parse_page(payload, page_url=START)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "archive.db"
        init_db(db, Path(self.temp.name) / "files")
        self.conn = get_connection(db)
        self.addCleanup(self.conn.close)

    def test_migration_10(self):
        self.assertIn(10, [v for v, _, _ in MIGRATIONS])

    def test_full_bounded_traversal_and_crosslink(self):
        self.conn.execute("INSERT INTO documents(url, download_status) VALUES(?, 'downloaded')",
                          ("https://cdn.muckrock.com/foia_files/2023/11/15/contracts.pdf",))
        self.conn.commit()
        fetch = Recorded()
        result = ingest(self.conn, fetch, max_pages=5)
        self.assertEqual(result["state"], "EXHAUSTED")
        self.assertEqual((result["requests"], result["releases_new"]), (3, 2))
        rows = self.conn.execute(
            "SELECT file_url, official_document_id, publisher, agency_ref FROM third_party_releases ORDER BY file_url"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        crosslinked = {r["file_url"]: r["official_document_id"] for r in rows}
        self.assertEqual(crosslinked["https://cdn.muckrock.com/foia_files/2023/11/15/contracts.pdf"], 1)
        # Third-party releases never become archived documents implicitly.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        report = {r["source_id"]: r for r in coverage_report(self.conn)}[source_id_for(API_ROOT)]
        self.assertEqual((report["unique_items"], report["declared_total_mismatch"]), (3, False))
        # Re-running an exhausted traversal does not refetch or duplicate.
        again = ingest(self.conn, fetch)
        self.assertEqual((again["pages_fetched"], len(fetch.calls)), (0, 2))
        inventory = build_source_inventory({}, conn=self.conn)
        muckrock = next(s for s in inventory["sources"] if s["publisher"] == "MuckRock")
        self.assertEqual(muckrock["enumeration_state"], "EXHAUSTED")

    def test_page_budget_resumes(self):
        fetch = Recorded()
        self.assertEqual(ingest(self.conn, fetch, max_pages=1)["state"], "IN_PROGRESS")
        self.assertEqual(ingest(self.conn, fetch, max_pages=1)["state"], "EXHAUSTED")
        self.assertEqual(len(fetch.calls), 2)

    def test_access_denial_is_blocked_not_evaded(self):
        fetch = Recorded(block=True)
        self.assertEqual(ingest(self.conn, fetch)["state"], "BLOCKED")
        self.assertEqual(ingest(self.conn, fetch)["state"], "BLOCKED")
        self.assertEqual(len(fetch.calls), 1)


if __name__ == "__main__":
    unittest.main()

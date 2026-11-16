"""Recent releases page and published-date filter on /api/v1/records."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from foia_archive.export_contract import export_page
from foia_archive.storage import get_connection, init_db, insert_document
from tests.test_export_contract import asgi_request
from ui import server

TODAY = datetime.now(timezone.utc).date()


def day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def build_db(root: Path) -> Path:
    db = root / "archive.db"
    init_db(db, root / "files")
    conn = get_connection(db)
    for name, published in (("fresh", day(1)), ("week-edge", day(7)), ("old", day(40)),
                            ("undated", None), ("future-dated", day(-3))):
        insert_document(conn, url=f"https://example.gov/{name}.pdf", title=f"Record {name}",
                        file_type="pdf", filename=f"{name}.pdf", agency_id=None, office_id=None,
                        reading_room_id=None, discovered_at="2026-11-01T00:00:00",
                        published_date=published)
    conn.close()
    return db


class RecentReleasesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = build_db(Path(tmp.name))
        patcher = patch("ui.server.get_db", side_effect=lambda: get_connection(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._EXPORT_RATE.clear()

    def get(self, path, query=b""):
        return asyncio.run(asgi_request(server.app, path, query))

    def test_recent_page_lists_window_newest_first_and_explains_exclusions(self):
        status, _, body = self.get("/recent", b"days=7")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("Record fresh", html)
        self.assertIn("Record week-edge", html)
        self.assertNotIn("Record old", html)
        self.assertNotIn("Record undated", html)
        self.assertIn("no release date at all", html)
        self.assertIn("only includes sources the archive has crawled", html)
        self.assertIn("(date shown on the source page)", html)
        self.assertLess(html.index("Record future-dated"), html.index("Record fresh"))

    def test_explicit_range_and_validation(self):
        status, _, body = self.get("/recent", f"since={day(10)}&until={day(5)}".encode())
        self.assertEqual(status, 200)
        self.assertIn("Record week-edge", body.decode())
        self.assertNotIn("Record fresh", body.decode())
        for query in (b"days=0", b"days=999", b"days=x", b"since=yesterday",
                      f"since={day(1)}&until={day(5)}".encode()):
            with self.subTest(query=query):
                self.assertEqual(self.get("/recent", query)[0], 400)

    def test_nav_links_to_recent(self):
        self.assertIn('href="/recent?days=7"', self.get("/recent")[2].decode())

    def test_records_api_filters_by_published_date(self):
        status, _, body = self.get("/api/v1/records", f"published_since={day(7)}".encode())
        page = json.loads(body)
        self.assertEqual(status, 200)
        titles = sorted(r["title"] for r in page["records"])
        self.assertEqual(titles, ["Record fresh", "Record future-dated", "Record week-edge"])
        self.assertEqual(page["filter"]["published_since"], day(7))
        status, _, body = self.get("/api/v1/records", f"published_since={day(10)}&published_until={day(5)}".encode())
        self.assertEqual([r["title"] for r in json.loads(body)["records"]], ["Record week-edge"])
        self.assertEqual(self.get("/api/v1/records", b"published_since=11/09/2026")[0], 400)

    def test_unfiltered_records_api_is_unchanged(self):
        page = json.loads(self.get("/api/v1/records")[2])
        self.assertNotIn("filter", page)
        self.assertEqual(len(page["records"]), 5)

    def test_filtered_cursor_pages_through_only_matching_records(self):
        conn = get_connection(self.db)
        try:
            first = export_page(conn, limit=1, published_since=day(7))
            second = export_page(conn, limit=5, cursor=first["next_cursor"], published_since=day(7))
        finally:
            conn.close()
        self.assertEqual(len(first["records"]) + len(second["records"]), 3)
        self.assertIsNone(second["next_cursor"])


if __name__ == "__main__":
    unittest.main()

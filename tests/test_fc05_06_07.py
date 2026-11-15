"""FC-05 backfill, FC-06 adapters, FC-07 content types (fixtures only)."""
import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from foia_archive.adapters import (
    detect_site_type, parse_json_listing, parse_sitemap, raise_if_blocked, sitemap_pager,
)
from foia_archive.backfill import backfill_status, missed_period_queue, plan_backfill, release_windows
from foia_archive.content_types import (
    UnsafeBundle, ZipLimits, extraction_outcome, format_coverage, safe_zip_members, sniff,
)
from foia_archive.enumeration import Page, PaginationError, enumerate_source
from foia_archive.scraper_core import SourceAccessBlocked
from foia_archive.storage import get_connection, init_db, upsert_reading_room

SITEMAPS = Path(__file__).parent / "fixtures" / "sitemaps"
ROOT = "https://foia.example.gov/sitemap.xml"


class DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "a.db"
        init_db(db, Path(self.temp.name) / "f")
        self.conn = get_connection(db)
        self.addCleanup(self.conn.close)


class BackfillTests(DbCase):
    def test_plan_is_idempotent_and_queue_is_oldest_first(self):
        self.assertEqual(plan_backfill(self.conn, "src_a", date(2018, 1, 1), date(2020, 12, 31)), 3)
        self.assertEqual(plan_backfill(self.conn, "src_a", date(2018, 1, 1), date(2020, 12, 31)), 0)
        plan_backfill(self.conn, "src_b", date(2019, 1, 1), date(2019, 12, 31))
        queue = missed_period_queue(self.conn)
        self.assertEqual([q["window_key"][:4] for q in queue], ["2018", "2019", "2019", "2020"])

    def test_windows_are_replayable_checkpoints(self):
        plan_backfill(self.conn, "src_a", date(2018, 1, 1), date(2019, 12, 31))
        enumerate_source(self.conn, "src_a", lambda c: Page([{"key": "k2018"}], None, total_hint=1),
                         initial_cursor={"page": 1}, strategy="date_window", window_key="2018-01-01..2018-12-31")
        status = backfill_status(self.conn, "src_a")
        self.assertEqual(status["states"], {"EXHAUSTED": 1, "PENDING": 1})
        self.assertEqual((status["verified_oldest"], status["verified_newest"]), ("2018-01-01", "2018-12-31"))
        self.assertEqual(status["expected_vs_seen"], [{"window": "2018-01-01..2018-12-31", "expected": 1, "seen": 1}])
        self.assertFalse(status["complete"])
        self.assertEqual([q["window_key"] for q in missed_period_queue(self.conn)], ["2019-01-01..2019-12-31"])

    def test_release_windows_show_empty_years(self):
        room = upsert_reading_room(self.conn, "https://foia.example.gov/rr", "RR", "agency", None, None)
        for i, published in enumerate(("2015-01-01", "2018-06-01", None)):
            self.conn.execute("INSERT INTO documents(id,url,published_date) VALUES(?,?,?)",
                              (i + 1, f"https://foia.example.gov/{i}", published))
            self.conn.execute("INSERT INTO document_sources(document_id,reading_room_id) VALUES(?,?)", (i + 1, room))
        self.conn.commit()
        window = release_windows(self.conn)[0]
        self.assertEqual((window["oldest_year"], window["newest_year"]), (2015, 2018))
        self.assertEqual(window["empty_years_in_span"], [2016, 2017])
        self.assertEqual(window["undated_records"], 1)


class AdapterTests(DbCase):
    def fetch(self, url):
        name = url.rsplit("/", 1)[-1]
        if name == "sitemap.xml":
            name = "index.xml"
        return 200, "application/xml", (SITEMAPS / name).read_bytes()

    def test_sitemap_tree_enumerates_same_host_records(self):
        result = enumerate_source(self.conn, "src_sm", sitemap_pager(self.fetch, ROOT),
                                  initial_cursor={"pending": [ROOT]}, strategy="sitemap", max_pages=10)
        self.assertEqual(result.state, "EXHAUSTED")
        keys = {r[0] for r in self.conn.execute("SELECT item_key FROM source_enumeration_items")}
        self.assertEqual(keys, {"https://foia.example.gov/docs/2019-a.pdf", "https://foia.example.gov/docs/2019-b.pdf",
                                "https://foia.example.gov/docs/2020-a.pdf"})

    def test_dtd_and_entities_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
        with self.assertRaises(PaginationError):
            parse_sitemap(bomb, ROOT)
        late = b"<?xml version='1.0'?><!--" + b"x" * 70000 + b"--><!DOCTYPE x><urlset/>"
        with self.assertRaises(PaginationError):
            parse_sitemap(late, ROOT)

    def test_challenge_pages_become_blocked(self):
        html = b"<html><title>Just a moment</title><div id='cf-chl-widget'>verify you are human</div></html>"
        self.assertEqual(detect_site_type("https://x.gov/", "text/html", html), "challenge")
        with self.assertRaises(SourceAccessBlocked):
            raise_if_blocked(200, "text/html", html)
        with self.assertRaises(SourceAccessBlocked):
            raise_if_blocked(403, "text/html", b"")
        raise_if_blocked(200, "text/html", b"<html><a href='/doc.pdf'>Release</a></html>")

    def test_site_type_detection(self):
        self.assertEqual(detect_site_type("https://x.gov/a", "application/json", b'{"results": []}'), "json_api")
        self.assertEqual(detect_site_type("https://x.gov/sitemap.xml", "text/xml", b"<urlset/>"), "sitemap")
        self.assertEqual(detect_site_type("https://x.gov/app/ReadingRoom.aspx", "text/html", b"<html>"), "pal")
        self.assertEqual(detect_site_type("https://x.gov/f", "", b"%PDF-1.7"), "document")

    def test_json_listing(self):
        payload = {"data": {"items": [{"file": {"href": "https://foia.example.gov/d1.pdf"}}, {"file": {}}]},
                   "links": {"next": "https://foia.example.gov/api?page=2"}, "meta": {"total": 2}}
        page = parse_json_listing(payload, source_url="https://foia.example.gov/api", items_path="data.items",
                                  url_field="file.href", next_path="links.next", total_path="meta.total")
        self.assertEqual(([i["key"] for i in page.items], page.next_cursor, page.total_hint),
                         (["https://foia.example.gov/d1.pdf"], {"url": "https://foia.example.gov/api?page=2"}, 2))
        with self.assertRaises(PaginationError):
            parse_json_listing({"data": {"items": []}, "next": "https://evil.example/x"},
                               source_url="https://foia.example.gov/api", items_path="data.items",
                               url_field="u", next_path="next")


def make_zip(members, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, body in members:
            archive.writestr(name, body)
    return buffer.getvalue()


class ContentTypeTests(DbCase):
    def test_sniff_by_signature(self):
        self.assertEqual(sniff(b"%PDF-1.4 x"), "application/pdf")
        docx = make_zip([("[Content_Types].xml", "<x/>"), ("word/document.xml", "<w/>")])
        self.assertTrue(sniff(docx).endswith("wordprocessingml.document"))
        self.assertEqual(sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 8), "application/x-ole-storage")
        self.assertEqual(sniff(b"\x89PNG\r\n\x1a\n...."), "image/png")
        self.assertEqual(sniff(b"<!DOCTYPE html><html>"), "text/html")
        self.assertEqual(sniff(b"\x00\xff\xfe\x80"), "application/octet-stream")

    def test_safe_zip_expansion_and_nesting(self):
        inner = make_zip([("b.pdf", b"%PDF-1.4 inner")])
        members = safe_zip_members(make_zip([("a.pdf", b"%PDF-1.4 a"), ("nested.zip", inner)]))
        self.assertEqual([m[0] for m in members], ["a.pdf", "nested.zip/b.pdf"])
        too_deep = make_zip([("n.zip", make_zip([("m.zip", inner)]))])
        with self.assertRaises(UnsafeBundle):
            safe_zip_members(too_deep)

    def test_bombs_and_traversal_rejected(self):
        with self.assertRaisesRegex(UnsafeBundle, "ratio"):
            safe_zip_members(make_zip([("z.txt", b"\0" * 5_000_000)]))
        with self.assertRaisesRegex(UnsafeBundle, "escapes"):
            safe_zip_members(make_zip([("../../etc/passwd", b"x")]))
        with self.assertRaisesRegex(UnsafeBundle, "Too many"):
            safe_zip_members(make_zip([(f"{i}.txt", b"x") for i in range(5)]), ZipLimits(max_members=4))
        with self.assertRaises(UnsafeBundle):
            safe_zip_members(b"PK\x03\x04 not really")

    def test_format_coverage_keeps_outcomes_distinct(self):
        rows = [("pdf", "indexed"), ("pdf", "ocr_failed"), ("pdf", None), ("docx", "empty"), ("wav", "unsupported")]
        for i, (fmt, status) in enumerate(rows, start=1):
            self.conn.execute("INSERT INTO documents(id,url,file_type,download_status) VALUES(?,?,?,'downloaded')",
                              (i, f"https://x.gov/{i}", fmt))
            if status:
                self.conn.execute("INSERT INTO document_text(document_id,body,extraction_status,character_count,truncated)"
                                  " VALUES(?,?,?,0,0)", (i, "", status))
        self.conn.commit()
        report = {r["format"]: r for r in format_coverage(self.conn)}
        self.assertEqual((report["pdf"]["indexed"], report["pdf"]["failed"], report["pdf"]["not_attempted"]), (1, 1, 1))
        self.assertEqual(report["pdf"]["indexed_pct_of_attempted"], 50.0)
        self.assertEqual(report["docx"]["empty"], 1)
        self.assertEqual(report["wav"]["unsupported"], 1)
        self.assertEqual(extraction_outcome("something_new"), "FAILED")
        self.assertEqual(extraction_outcome(None), "NOT_ATTEMPTED")


if __name__ == "__main__":
    unittest.main()

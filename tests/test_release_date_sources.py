"""Release-date sources (page > Last-Modified > PDF metadata) and newly-found baselines."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from foia_archive import published_window as pw
from foia_archive.export_contract import export_page
from foia_archive.scraper_core import (
    DownloadResult,
    _head_last_modified,
    _parse_http_date,
    _pdf_metadata_date,
    crawl_reading_room,
)
from foia_archive.storage import (
    get_connection,
    init_db,
    insert_document,
    set_document_date,
    upsert_reading_room,
)
from foia_archive.utils import Config
from tests.test_export_contract import asgi_request
from ui import server

ROOT = "https://example.gov/reading-room/"
TODAY = datetime.now(timezone.utc).date()


def http_date(days_ago):
    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return stamp.strftime("%a, %d %b %Y %H:%M:%S GMT")


class Page:
    def __init__(self, html):
        self.status_code, self.headers, self.encoding = 200, {"Content-Type": "text/html"}, "utf-8"
        self._body = html.encode()

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class Head:
    def __init__(self, status=200, headers=None):
        self.status_code, self.headers = status, headers or {}

    def close(self):
        pass


class CrawlBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db, self.files = root / "a.db", root / "files"
        init_db(self.db, self.files)
        conn = get_connection(self.db)
        self.rr_id = upsert_reading_room(conn, ROOT, "Room", "office", None, None, source_type="reading_room")
        conn.close()
        patcher = patch("foia_archive.scraper_core._validate_public_destination", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, window=True, probes=0):
        crawler = {"user_agent": "Test/1.0", "max_pages_per_source": 1, "max_depth": 0,
                   "max_discovered_docs_per_source": 50, "page_timeout_seconds": 5,
                   "page_max_size_mb": 1, "per_host_delay_seconds": 0,
                   "max_head_date_probes_per_source": probes}
        if window:
            crawler["published_window"] = {"since": (TODAY - timedelta(days=7)).isoformat(),
                                           "until": None, "include_unknown": False}
        return Config({"crawler": crawler,
                       "downloader": {"max_redirects": 2, "max_retries": 0, "retry_backoff_seconds": 0},
                       "storage": {"db_path": str(self.db), "files_dir": str(self.files)}})

    def crawl(self, html, config, heads=None, dry_run=False):
        downloads, head_calls = [], []

        def fake_get(url, **kwargs):
            assert url == ROOT, url
            return Page(html)

        def fake_head(url, **kwargs):
            head_calls.append(url)
            return (heads or {}).get(url.rsplit("/", 1)[-1], Head(405))

        def fake_download(url, hint, cfg, **kwargs):
            downloads.append(url.rsplit("/", 1)[-1])
            return DownloadResult(status="retryable_error", error="fixture")

        with patch("foia_archive.scraper_core.requests.get", side_effect=fake_get), \
                patch("foia_archive.scraper_core.requests.head", side_effect=fake_head), \
                patch("foia_archive.scraper_core.download_document", side_effect=fake_download):
            crawl_reading_room(self.rr_id, config, dry_run=dry_run, max_docs=20)
        return downloads, head_calls

    def rows(self):
        conn = get_connection(self.db)
        try:
            return {r["url"].rsplit("/", 1)[-1]: dict(r) for r in conn.execute(
                "SELECT url, published_date, published_date_source, newly_found_at FROM documents")}
        finally:
            conn.close()


class NewlyFoundTests(CrawlBase):
    def test_baseline_crawl_never_floods_newly_found(self):
        downloads, _ = self.crawl('<a href="a.pdf">A</a><a href="b.pdf">B</a>', self.config())
        self.assertEqual(downloads, [])
        self.assertTrue(all(r["newly_found_at"] is None for r in self.rows().values()))
        conn = get_connection(self.db)
        baseline = conn.execute("SELECT baseline_completed_at FROM reading_rooms").fetchone()[0]
        conn.close()
        self.assertIsNotNone(baseline)

    def test_records_first_seen_after_baseline_are_newly_found_and_downloaded(self):
        config = self.config()
        self.crawl('<a href="a.pdf">A</a>', config)
        downloads, _ = self.crawl('<a href="a.pdf">A</a><a href="new.pdf">New</a>', config)
        rows = self.rows()
        self.assertEqual(downloads, ["new.pdf"])
        self.assertIsNone(rows["a.pdf"]["newly_found_at"])
        self.assertTrue(rows["new.pdf"]["newly_found_at"].startswith(TODAY.isoformat()))
        self.assertEqual(pw.stats(config)["newly_found"], 1)


class LastModifiedTests(CrawlBase):
    def test_head_probe_dates_undated_links_within_budget(self):
        heads = {"a.pdf": Head(200, {"Last-Modified": http_date(2)}),
                 "b.pdf": Head(200, {"Last-Modified": http_date(400)}),
                 "c.pdf": Head(200, {"Last-Modified": http_date(1)})}
        html = ('<a href="a.pdf">A</a><a href="b.pdf">B</a><a href="c.pdf">C</a>'
                '<a href="d.pdf" data-published-date="2026-01-02">D</a>')
        downloads, head_calls = self.crawl(html, self.config(probes=2), heads=heads)
        rows = self.rows()
        self.assertEqual([u.rsplit("/", 1)[-1] for u in head_calls], ["a.pdf", "b.pdf"])  # budget 2, d dated
        self.assertEqual(rows["a.pdf"]["published_date_source"], "header")
        self.assertEqual(rows["d.pdf"]["published_date_source"], "page")
        self.assertIsNone(rows["c.pdf"]["published_date"])
        self.assertEqual(downloads, ["a.pdf"])

    def test_probes_are_off_by_default_and_failures_yield_no_date(self):
        _, head_calls = self.crawl('<a href="a.pdf">A</a>', self.config(probes=0))
        self.assertEqual(head_calls, [])
        with patch("foia_archive.scraper_core.requests.head", side_effect=OSError("down")):
            self.assertIsNone(_head_last_modified("https://example.gov/x.pdf", self.config(), None))

    def test_http_date_parsing_rejects_future_and_garbage(self):
        self.assertEqual(_parse_http_date("Tue, 10 Nov 2026 08:00:00 GMT"), "2026-11-10")
        self.assertIsNone(_parse_http_date(http_date(-30)))
        self.assertIsNone(_parse_http_date("yesterday"))


class RankingAndMetadataTests(unittest.TestCase):
    def test_page_outranks_header_outranks_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_db(Path(tmp) / "a.db", Path(tmp) / "f")
            conn = get_connection(Path(tmp) / "a.db")
            insert_document(conn, url="https://e.gov/a.pdf", title="A", file_type="pdf", filename="a.pdf",
                            agency_id=None, office_id=None, reading_room_id=None,
                            discovered_at="2026-11-01T00:00:00")
            self.assertTrue(set_document_date(conn, "https://e.gov/a.pdf", "2026-11-03", "metadata"))
            self.assertTrue(set_document_date(conn, "https://e.gov/a.pdf", "2026-11-04", "header"))
            self.assertFalse(set_document_date(conn, "https://e.gov/a.pdf", "2026-11-05", "metadata"))
            self.assertTrue(set_document_date(conn, "https://e.gov/a.pdf", "2026-11-02", "page"))
            self.assertFalse(set_document_date(conn, "https://e.gov/a.pdf", "2026-11-06", "header"))
            row = conn.execute("SELECT published_date, published_date_source FROM documents").fetchone()
            self.assertEqual(tuple(row), ("2026-11-02", "page"))
            conn.close()

    def test_pdf_metadata_date(self):
        writer = PdfWriter()
        writer.add_blank_page(72, 72)
        writer.add_metadata({"/ModDate": "D:20261112093000-05'00'", "/CreationDate": "D:20200101"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.pdf"
            with path.open("wb") as handle:
                writer.write(handle)
            self.assertEqual(_pdf_metadata_date(path), "2026-11-12")
            (Path(tmp) / "bad.pdf").write_bytes(b"not a pdf")
            self.assertIsNone(_pdf_metadata_date(Path(tmp) / "bad.pdf"))

    def test_migration_backfills_baseline_and_page_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            init_db(db, Path(tmp) / "f")
            conn = get_connection(db)
            conn.execute("UPDATE reading_rooms SET baseline_completed_at = NULL")
            upsert_reading_room(conn, ROOT, "R", "office", None, None)
            conn.execute("UPDATE reading_rooms SET last_successful_crawl_at = '2026-10-01T00:00:00'")
            conn.execute("DELETE FROM schema_migrations WHERE version = 15")
            conn.commit()
            conn.close()
            init_db(db, Path(tmp) / "f")
            conn = get_connection(db)
            self.assertEqual(conn.execute("SELECT baseline_completed_at FROM reading_rooms").fetchone()[0],
                             "2026-10-01T00:00:00")
            conn.close()


class RecentBasisTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "a.db"
        init_db(self.db, Path(tmp.name) / "f")
        conn = get_connection(self.db)
        now = datetime.now(timezone.utc)
        for name, date, source, newly in (
                ("dated", (TODAY - timedelta(days=2)).isoformat(), "header", None),
                ("fresh", None, None, (now - timedelta(days=1)).isoformat()),
                ("baseline", None, None, None),
                ("old-new", None, None, (now - timedelta(days=30)).isoformat())):
            insert_document(conn, url=f"https://e.gov/{name}.pdf", title=f"Rec {name}", file_type="pdf",
                            filename=f"{name}.pdf", agency_id=None, office_id=None, reading_room_id=None,
                            discovered_at=now.isoformat(), published_date=date,
                            published_date_source=source, newly_found_at=newly)
        conn.close()
        patcher = patch("ui.server.get_db", side_effect=lambda: get_connection(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._EXPORT_RATE.clear()

    def get(self, path, query=b""):
        return asyncio.run(asgi_request(server.app, path, query))

    def test_recent_defaults_to_dated_or_newly_found_with_labels(self):
        html = self.get("/recent", b"days=7")[2].decode()
        self.assertIn("Rec dated", html)
        self.assertIn("Rec fresh", html)
        self.assertNotIn("Rec baseline", html)
        self.assertNotIn("Rec old-new", html)
        self.assertIn("(server Last-Modified date)", html)
        self.assertIn("Newly found", html)
        self.assertIn("(first seen by our crawler)", html)
        self.assertNotIn("Rec fresh", self.get("/recent", b"days=7&basis=dated")[2].decode())
        self.assertNotIn("Rec dated", self.get("/recent", b"days=7&basis=new")[2].decode())
        self.assertEqual(self.get("/recent", b"basis=all")[0], 400)

    def test_records_api_explains_each_match(self):
        since = (TODAY - timedelta(days=7)).isoformat()
        page = json.loads(self.get("/api/v1/records", f"published_since={since}".encode())[2])
        self.assertEqual(sorted(r["title"] for r in page["records"]), ["Rec dated", "Rec fresh"])
        bases = sorted((v["basis"], v["date_source"]) for v in page["filter"]["date_basis"].values())
        self.assertEqual(bases, [("dated", "header"), ("newly_found", "first_seen")])
        self.assertEqual(self.get("/api/v1/records", f"published_since={since}&basis=x".encode())[0], 400)
        conn = get_connection(self.db)
        try:
            only_dated = export_page(conn, published_since=since, basis="dated")
        finally:
            conn.close()
        self.assertEqual([r["title"] for r in only_dated["records"]], ["Rec dated"])


if __name__ == "__main__":
    unittest.main()

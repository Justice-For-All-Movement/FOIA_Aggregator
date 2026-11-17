"""Published FOIA log scraper with fake fetchers (no network)."""
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path

from foia_archive.published_logs import LogSource, find_log_links, load_registry, scrape_source
from foia_archive.scraper_core import SourceAccessBlocked

FIXTURE = Path(__file__).parent / "fixtures" / "published_logs" / "closed_log_q3.csv"
INDEX = """<html><body><ul>
<li><a href="/foia/logs/fy2025-q4-foia-log.csv">FY2025 Q4 FOIA Log</a></li>
<li><a href="/foia/logs/fy2026-q1-foia-log.csv">FY2026 Q1 FOIA Log</a></li>
<li><a href="/foia/annual-report-2025.pdf">Annual report</a></li>
<li><a href="https://other.example.org/request-log.xlsx">Request log mirror</a></li>
<li><a href="/media/12345">Quarterly FOIA log (XLSX)</a></li>
</ul></body></html>"""


def memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class FindLinksTests(unittest.TestCase):
    def test_log_links_newest_first(self):
        links = find_log_links(INDEX, "https://foia.example.gov/foia/logs")
        urls = [l["url"] for l in links]
        self.assertEqual(urls[0], "https://foia.example.gov/foia/logs/fy2026-q1-foia-log.csv")
        self.assertIn("https://foia.example.gov/media/12345", urls)
        self.assertNotIn("https://foia.example.gov/foia/annual-report-2025.pdf", urls)


class ScrapeTests(unittest.TestCase):
    source = LogSource(source_id="example", agency_slug="example-department",
                       index_url="https://foia.example.gov/foia/logs", cadence="quarterly")

    def test_ingests_and_is_idempotent(self):
        conn = memory_conn()
        page = lambda url: (200, url, INDEX)
        file = lambda url: (200, url, FIXTURE.read_bytes())
        first = scrape_source(conn, self.source, page, file, max_files=1)
        self.assertEqual(first["state"], "OK")
        self.assertEqual(first["files"][0]["state"], "ingested")
        second = scrape_source(conn, self.source, page, file, max_files=1)
        self.assertEqual(second["files"][0]["state"], "unchanged")
        row = conn.execute("SELECT state, files_ingested FROM published_log_sources").fetchone()
        self.assertEqual(tuple(row), ("OK", 0))

    def test_blocked_and_captcha_are_recorded_not_evaded(self):
        conn = memory_conn()

        def forbidden(url):
            raise SourceAccessBlocked("HTTP 403 from " + url)

        self.assertEqual(scrape_source(conn, self.source, forbidden, None)["state"], "BLOCKED")
        captcha = lambda url: (200, url, "<html>Please complete the CAPTCHA</html>")
        self.assertEqual(scrape_source(conn, self.source, captcha, None)["state"], "BLOCKED")

    def test_dry_run_downloads_nothing(self):
        def no_file(url):
            raise AssertionError("dry run must not download")
        result = scrape_source(memory_conn(), self.source, lambda u: (200, u, INDEX), no_file, dry_run=True)
        self.assertTrue(all(f["state"] == "found" for f in result["files"]))


class RegistryTests(unittest.TestCase):
    def test_repository_registry_is_valid(self):
        sources = load_registry(Path(__file__).parent.parent / "config" / "published_logs.yaml")
        self.assertTrue(sources)
        self.assertEqual(len({s.source_id for s in sources}), len(sources))

    def test_rejects_plain_http_and_bad_cadence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "r.yaml")
            path.write_text("sources:\n  - {agency_slug: x, index_url: 'http://x.gov/logs'}\n")
            with self.assertRaises(ValueError):
                load_registry(path)
            path.write_text("sources:\n  - {agency_slug: x, index_url: 'https://x.gov/logs', cadence: hourly}\n")
            with self.assertRaises(ValueError):
                load_registry(path)


if __name__ == "__main__":
    unittest.main()


class SubpageTests(unittest.TestCase):
    def test_follows_one_level_of_log_pages(self):
        from foia_archive.published_logs import find_log_pages
        index = '<a href="/foia/requests-closed-2026">Requests closed in 2026</a><a href="https://x.org/log">x</a>'
        sub = '<a href="/media/1/dl">Requests Closed by OIP October 2026</a>'
        self.assertEqual(find_log_pages(index, "https://foia.example.gov/foia"),
                         ["https://foia.example.gov/foia/requests-closed-2026"])
        pages = {"https://foia.example.gov/foia": index, "https://foia.example.gov/foia/requests-closed-2026": sub}
        source = LogSource(source_id="s", agency_slug="a", index_url="https://foia.example.gov/foia")
        result = scrape_source(memory_conn(), source, lambda u: (200, u, pages[u]), None, dry_run=True)
        self.assertEqual(result["files_found"], 1)

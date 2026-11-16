"""Broad coverage: scoped document links, page-level baselines, feeds,
robots.txt, HEAD sizes, linked PAL rooms and host-parallel crawling."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from foia_archive.feeds import discover_feed_urls, parse_feed
from foia_archive.robots import RobotsPolicy
from foia_archive.scraper_core import (
    DownloadResult,
    HostRateLimiter,
    RobotsDisallowed,
    _generated_on_request,
    _request_with_safe_redirects,
    crawl_reading_room,
    document_host_filter,
    extract_document_links,
    registrable_domain,
)
from foia_archive.storage import get_connection, init_db, upsert_reading_room
from foia_archive.utils import Config

ROOT = "https://example.gov/reading-room/"
TODAY = datetime.now(timezone.utc).date()


class Resp:
    def __init__(self, body="", ctype="text/html", status=200, headers=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self.encoding = "utf-8"
        self._body = body.encode() if isinstance(body, str) else body

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class ScopeTests(unittest.TestCase):
    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("vault.fbi.gov"), "fbi.gov")
        self.assertEqual(registrable_domain("www.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("fbijobs.gov"), "fbijobs.gov")

    def test_offsite_footer_documents_are_dropped(self):
        allowed = document_host_filter(["https://vault.fbi.gov/"])
        html = ('<a href="/a.pdf">A</a><a href="https://www.fbi.gov/b.pdf">B</a>'
                '<a href="https://fbijobs.gov/sites/default/files/eeoc_policy.pdf">Jobs</a>'
                '<a href="https://media.defense.gov/2026/x.pdf">DoD</a>')
        urls = [link["url"] for link in extract_document_links(html, "https://vault.fbi.gov/", allowed)]
        self.assertEqual(urls, ["https://vault.fbi.gov/a.pdf", "https://www.fbi.gov/b.pdf",
                                "https://media.defense.gov/2026/x.pdf"])


class CrawlCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db, self.files = self.root / "a.db", self.root / "files"
        init_db(self.db, self.files)
        conn = get_connection(self.db)
        self.rr_id = upsert_reading_room(conn, ROOT, "Room", "office", None, None, source_type="reading_room")
        conn.close()
        patcher = patch("foia_archive.scraper_core._validate_public_destination", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, depth=1, pages=10, **crawler):
        base = {"user_agent": "Test/1.0", "max_pages_per_source": pages, "max_depth": depth,
                "max_discovered_docs_per_source": 50, "page_timeout_seconds": 5,
                "page_max_size_mb": 1, "per_host_delay_seconds": 0}
        base.update(crawler)
        return Config({"crawler": base,
                       "downloader": {"max_redirects": 2, "max_retries": 0, "retry_backoff_seconds": 0},
                       "storage": {"db_path": str(self.db), "files_dir": str(self.files)}})

    def crawl(self, pages, config, heads=None):
        def fake_get(url, **kwargs):
            if url not in pages:
                return Resp("missing", status=404)
            body = pages[url]
            return body if isinstance(body, Resp) else Resp(body)

        def fake_head(url, **kwargs):
            return (heads or {}).get(url, Resp(status=405))

        with patch("foia_archive.scraper_core.requests.get", side_effect=fake_get), \
                patch("foia_archive.scraper_core.requests.head", side_effect=fake_head), \
                patch("foia_archive.scraper_core.download_document",
                      return_value=DownloadResult(status="retryable_error", error="fixture")):
            crawl_reading_room(self.rr_id, config, dry_run=True, max_docs=50)

    def rows(self):
        conn = get_connection(self.db)
        try:
            return {r["url"].rsplit("/", 1)[-1]: dict(r) for r in conn.execute(
                "SELECT url, published_date, published_date_source, newly_found_at, "
                "remote_content_length FROM documents")}
        finally:
            conn.close()


class PageBaselineTests(CrawlCase):
    def test_deeper_pages_than_baseline_are_not_newly_found(self):
        root = '<a href="a.pdf">A</a><a href="page2">Older releases</a>'
        self.crawl({ROOT: root}, self.config(depth=0))           # baseline: root only
        pages = {ROOT: root + '<a href="fresh.pdf">Fresh</a>',
                 ROOT + "page2": '<a href="old-deep.pdf">Old deep</a>'}
        self.crawl(pages, self.config(depth=1))                  # reaches page2 first time
        rows = self.rows()
        self.assertIsNotNone(rows["fresh.pdf"]["newly_found_at"])
        self.assertIsNone(rows["old-deep.pdf"]["newly_found_at"])
        pages[ROOT + "page2"] += '<a href="later-deep.pdf">Later</a>'
        self.crawl(pages, self.config(depth=1))                  # page2 now in baseline
        self.assertIsNotNone(self.rows()["later-deep.pdf"]["newly_found_at"])

    def test_legacy_baseline_without_page_rows_trusts_root_only(self):
        conn = get_connection(self.db)
        conn.execute("UPDATE reading_rooms SET baseline_completed_at = '2026-10-01T00:00:00'")
        conn.commit()
        conn.close()
        self.crawl({ROOT: '<a href="n.pdf">N</a><a href="p2">p2</a>',
                    ROOT + "p2": '<a href="d.pdf">D</a>'}, self.config(depth=1))
        rows = self.rows()
        self.assertIsNotNone(rows["n.pdf"]["newly_found_at"])
        self.assertIsNone(rows["d.pdf"]["newly_found_at"])


class FeedTests(CrawlCase):
    FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Release one</title><link>https://example.gov/reading-room/release-one</link>
        <pubDate>{d1}</pubDate></item>
      <item><title>Direct PDF</title><link>https://example.gov/reading-room/direct.pdf</link>
        <pubDate>{d2}</pubDate></item>
      <item><title>Elsewhere</title><link>https://example.gov/news/x</link></item>
    </channel></rss>"""

    def test_parse_rss_and_atom(self):
        items = parse_feed(self.FEED.format(d1="Mon, 16 Nov 2026 00:00:00 -0500",
                                            d2="Tue, 10 Nov 2026 08:00:00 GMT"), ROOT)
        self.assertEqual([i["published_date"] for i in items], ["2026-11-16", "2026-11-10", None])
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
                '<link href="/r/x.pdf"/><updated>2026-11-12T10:00:00Z</updated></entry></feed>')
        self.assertEqual(parse_feed(atom, ROOT)[0]["url"], "https://example.gov/r/x.pdf")
        with self.assertRaises(ValueError):
            parse_feed('<!DOCTYPE x [<!ENTITY a "b">]><rss/>', ROOT)

    def test_discovered_feed_dates_documents_and_item_pages(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%a, %d %b %Y 00:00:00 GMT")
        older = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%a, %d %b %Y 00:00:00 GMT")
        root = '<html><head><link rel="alternate" type="application/rss+xml" href="feed.xml"></head></html>'
        from bs4 import BeautifulSoup
        self.assertEqual(discover_feed_urls(BeautifulSoup(root, "html.parser"), ROOT), [ROOT + "feed.xml"])
        pages = {ROOT: root,
                 ROOT + "feed.xml": Resp(self.FEED.format(d1=recent, d2=older), "application/rss+xml"),
                 ROOT + "release-one": '<a href="attachment.pdf">Attachment</a>'}
        self.crawl(pages, self.config(depth=1))
        rows = self.rows()
        self.assertEqual(rows["direct.pdf"]["published_date"], (TODAY - timedelta(days=3)).isoformat())
        self.assertEqual(rows["attachment.pdf"]["published_date"], (TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(rows["attachment.pdf"]["published_date_source"], "page")


class HeadProbeTests(CrawlCase):
    def test_sizes_recorded_and_dynamic_last_modified_ignored(self):
        now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        heads = {ROOT + "a.pdf": Resp(status=200, headers={"Content-Length": "1234",
                                                             "Last-Modified": now, "Date": now}),
                 ROOT + "b.pdf": Resp(status=200, headers={"Content-Length": "99"})}
        self.crawl({ROOT: '<a href="a.pdf">A</a><a href="b.pdf" data-published-date="2026-01-02">B</a>'},
                   self.config(max_head_date_probes_per_source=5, head_probe_sizes=True), heads)
        rows = self.rows()
        self.assertEqual(rows["a.pdf"]["remote_content_length"], 1234)
        self.assertIsNone(rows["a.pdf"]["published_date"])   # stamp == Date header: generated
        self.assertEqual(rows["b.pdf"]["remote_content_length"], 99)
        self.assertTrue(_generated_on_request(now, now))
        self.assertFalse(_generated_on_request("Tue, 10 Nov 2026 08:00:00 GMT", now))


class LinkedPalTests(CrawlCase):
    def test_same_domain_pal_reading_room_is_registered(self):
        self.crawl({ROOT: '<a href="https://securefoia.example.gov/app/ReadingRoom.aspx">PAL</a>'
                          '<a href="https://other.gov/app/ReadingRoom.aspx">Other</a>'},
                   self.config(depth=0))
        conn = get_connection(self.db)
        rooms = {r["url"]: r["source_type"] for r in conn.execute("SELECT url, source_type FROM reading_rooms")}
        conn.close()
        self.assertEqual(rooms.get("https://securefoia.example.gov/app/ReadingRoom.aspx"), "linked_pal")
        self.assertNotIn("https://other.gov/app/ReadingRoom.aspx", rooms)


class RobotsTests(unittest.TestCase):
    def policy(self, status, text=""):
        return RobotsPolicy(fetch=lambda url, ua: (status, text))

    def test_disallow_crawl_delay_and_failures(self):
        robots = "User-agent: *\nDisallow: /private/\nCrawl-delay: 5\n"
        policy = self.policy(200, robots)
        self.assertTrue(policy.allowed("https://e.gov/foia/", "FOIAArchiveBot/0.1"))
        self.assertFalse(policy.allowed("https://e.gov/private/x.pdf", "FOIAArchiveBot/0.1"))
        self.assertEqual(policy.crawl_delay("https://e.gov/", "FOIAArchiveBot/0.1"), 5.0)
        self.assertTrue(self.policy(404).allowed("https://e.gov/x", "Bot/1"))
        self.assertFalse(self.policy(503).allowed("https://e.gov/x", "Bot/1"))

        def boom(url, ua):
            raise OSError("unreachable")
        self.assertFalse(RobotsPolicy(fetch=boom).allowed("https://e.gov/x", "Bot/1"))

    def test_requests_are_gated_and_delayed(self):
        policy = self.policy(200, "User-agent: *\nDisallow: /no/\nCrawl-delay: 3\n")
        limiter = HostRateLimiter(0.5)
        with patch("foia_archive.scraper_core._validate_public_destination", return_value=None), \
                patch("foia_archive.scraper_core.requests.get", return_value=Resp("ok")):
            with self.assertRaises(RobotsDisallowed):
                _request_with_safe_redirects("https://e.gov/no/x", headers={"User-Agent": "Bot/1"},
                                             timeout=1, max_redirects=0, robots=policy)
            _request_with_safe_redirects("https://e.gov/yes", headers={"User-Agent": "Bot/1"},
                                         timeout=1, max_redirects=0, rate_limiter=limiter, robots=policy)
        self.assertEqual(limiter.delay_for("https://e.gov/other"), 3.0)


class ParallelEngineTests(unittest.TestCase):
    def test_sources_on_different_domains_are_all_crawled(self):
        from foia_archive.engine import run_once
        with tempfile.TemporaryDirectory() as tmp:
            db, files = Path(tmp) / "a.db", Path(tmp) / "f"
            init_db(db, files)
            conn = get_connection(db)
            roots = [f"https://site{i}.gov/foia/" for i in range(4)] + ["https://www.site0.gov/foia2/"]
            for url in roots:
                upsert_reading_room(conn, url, url, "office", None, None, source_type="reading_room")
            conn.close()
            cfg = {"crawler": {"user_agent": "Test/1.0", "max_pages_per_source": 1, "max_depth": 0,
                               "per_host_delay_seconds": 0, "parallel_hosts": 3,
                               "max_head_date_probes_per_source": 0, "dry_run": True},
                   "downloader": {"max_redirects": 1, "max_retries": 0},
                   "storage": {"backend": "local", "db_path": str(db), "files_dir": str(files)},
                   "database_backup": {"enabled": False}}
            path = Path(tmp) / "c.yaml"
            path.write_text(yaml.safe_dump(cfg))

            def fake_get(url, **kwargs):
                return Resp(f'<a href="{url}doc.pdf">Doc</a>')

            with patch("foia_archive.scraper_core._validate_public_destination", return_value=None), \
                    patch("foia_archive.scraper_core.requests.get", side_effect=fake_get):
                run_once(config_path=str(path), dry_run=True, max_docs_per_source=5,
                         refresh_metadata_enabled=False, backup_enabled=False)
            conn = get_connection(db)
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            ok = conn.execute("SELECT COUNT(*) FROM reading_rooms WHERE last_successful_crawl_at IS NOT NULL").fetchone()[0]
            conn.close()
            self.assertEqual((docs, ok), (5, 5))


if __name__ == "__main__":
    unittest.main()

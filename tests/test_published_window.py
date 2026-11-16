"""Published-date window crawl ("everything released in the last week")."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from foia_archive import published_window as pw
from foia_archive.ingestion_journal import policy_fingerprint
from foia_archive.scraper_core import DownloadResult, crawl_reading_room
from foia_archive.storage import get_connection, init_db, upsert_reading_room
from foia_archive.utils import Config, load_config

ROOT = "https://example.gov/reading-room/"
PAGE = """
<ul>
  <li><a href="old.pdf" data-published-date="2026-01-05">Old release</a></li>
  <li><a href="undated.pdf">Undated release</a></li>
  <li><a href="new-a.pdf" data-published-date="2026-11-12">New A</a></li>
  <li><a href="later.pdf" data-published-date="2026-12-01">After window</a></li>
  <li><a href="new-b.pdf" data-published-date="November 14, 2026">New B</a></li>
</ul>
"""


class FakeResponse:
    def __init__(self, text):
        self.status_code = 200
        self.headers = {"Content-Type": "text/html"}
        self.encoding = "utf-8"
        self._body = text.encode()

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class ResolveWindowTests(unittest.TestCase):
    def test_since_days_is_relative_to_today(self):
        window = pw.resolve_window(since_days=7, today=date(2026, 11, 16))
        self.assertEqual(window, {"since": "2026-11-09", "until": None, "include_unknown": False})

    def test_no_bounds_means_no_window(self):
        self.assertIsNone(pw.resolve_window())

    def test_invalid_inputs_fail_closed(self):
        for kwargs in ({"since": "yesterday"}, {"since": "2026-11-10", "until": "2026-11-01"},
                       {"since": "2026-11-01", "since_days": 3}, {"since_days": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pw.resolve_window(**kwargs)

    def test_classification(self):
        window = pw.PublishedWindow("2026-11-09", "2026-11-16")
        self.assertEqual(window.classify("2026-11-09"), pw.IN_WINDOW)
        self.assertEqual(window.classify("2026-11-16T23:00:00"), pw.IN_WINDOW)
        self.assertEqual(window.classify("2026-11-08"), pw.OUT_OF_WINDOW)
        self.assertEqual(window.classify(None), pw.UNKNOWN)
        self.assertFalse(window.admits(None))
        self.assertTrue(pw.PublishedWindow("2026-11-09", None, True).admits(None))

    def test_fingerprint_unchanged_without_window(self):
        base = Config({"crawler": {"dry_run": False}, "storage": {}})
        same = Config({"crawler": {"dry_run": False, "published_window": None}, "storage": {}})
        windowed = Config({"crawler": {"dry_run": False, "published_window": {"since": "2026-11-09"}},
                           "storage": {}})
        self.assertEqual(policy_fingerprint(base), policy_fingerprint(same))
        self.assertNotEqual(policy_fingerprint(base), policy_fingerprint(windowed))

    def test_environment_window_for_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text("crawler: {}\n")
            with patch.dict(os.environ, {"FOIA_PUBLISHED_SINCE": "2026-11-09",
                                         "FOIA_PUBLISHED_INCLUDE_UNKNOWN": "true"}):
                cfg = load_config(str(path))
        self.assertEqual(cfg.crawler["published_window"],
                         {"since": "2026-11-09", "until": None, "include_unknown": True})

    def test_cli_rejects_bad_dates_before_any_work(self):
        import main
        with patch.object(main, "run_once") as run_once:
            result = CliRunner().invoke(main.app, ["run", "--published-since", "last week"])
        self.assertNotEqual(result.exit_code, 0)
        run_once.assert_not_called()

    def test_cli_passes_window_and_prints_summary(self):
        import main

        def fake_run_once(**kwargs):
            kwargs["window_stats_out"].update({"in_window": 2, "window": kwargs["published_window"]})
            return True

        with patch.object(main, "run_once", side_effect=fake_run_once) as run_once:
            result = CliRunner().invoke(main.app, ["run", "--published-since", "2026-11-09",
                                                   "--published-until", "2026-11-16"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run_once.call_args.kwargs["published_window"],
                         {"since": "2026-11-09", "until": "2026-11-16", "include_unknown": False})
        self.assertIn('"in_window": 2', result.output)


class LabeledTextDateTests(unittest.TestCase):
    def test_only_labeled_listing_dates_are_published_dates(self):
        from foia_archive.scraper_core import extract_document_links
        html = """<ul>
          <li><a href="a.pdf">A</a> (Posted 11/12/2026)</li>
          <li><a href="b.pdf">B</a> Date Released: Sept. 3, 2026</li>
          <li><a href="c.pdf">Memo</a> - Jan. 30, 2025</li>
          <li><a href="d.pdf">D</a> released 11/1/2026, re-released 11/2/2026</li>
        </ul>"""
        dates = {link["url"].rsplit("/", 1)[-1]: link["published_date"]
                 for link in extract_document_links(html, "https://example.gov/")}
        self.assertEqual(dates, {"a.pdf": "2026-11-12", "b.pdf": "2026-09-03",
                                 "c.pdf": None, "d.pdf": None})


class WindowCrawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path, self.files_dir = root / "archive.db", root / "files"
        init_db(self.db_path, self.files_dir)
        conn = get_connection(self.db_path)
        self.rr_id = upsert_reading_room(conn, ROOT, "Room", "office", None, None, source_type="reading_room")
        conn.close()
        patcher = patch("foia_archive.scraper_core._validate_public_destination", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _config(self, **window):
        return Config({
            "crawler": {"user_agent": "FOIAArchiveTest/1.0", "max_pages_per_source": 2, "max_depth": 0,
                        "max_discovered_docs_per_source": 20, "page_timeout_seconds": 5,
                        "page_max_size_mb": 1, "per_host_delay_seconds": 0,
                        "published_window": {"since": "2026-11-09", "until": "2026-11-16", **window}},
            "downloader": {"max_redirects": 2, "max_retries": 0, "retry_backoff_seconds": 0},
            "storage": {"db_path": str(self.db_path), "files_dir": str(self.files_dir)},
        })

    def _crawl(self, config, max_docs=10):
        downloads = []

        def fake_download(url, filename_hint, cfg, **kwargs):
            downloads.append(url.rsplit("/", 1)[-1])
            return DownloadResult(status="retryable_error", error="fixture")

        def fake_get(url, **kwargs):
            if url != ROOT:
                raise AssertionError(f"unexpected page fetch {url}")
            return FakeResponse(PAGE)

        with patch("foia_archive.scraper_core.requests.get", side_effect=fake_get), \
                patch("foia_archive.scraper_core.download_document", side_effect=fake_download):
            crawl_reading_room(self.rr_id, config, dry_run=False, max_docs=max_docs)
        conn = get_connection(self.db_path)
        try:
            rows = {r["url"].rsplit("/", 1)[-1]: r["published_date"]
                    for r in conn.execute("SELECT url, published_date FROM documents")}
        finally:
            conn.close()
        return downloads, rows

    def test_only_in_window_documents_are_downloaded_but_all_metadata_recorded(self):
        config = self._config()
        downloads, rows = self._crawl(config)
        self.assertEqual(downloads, ["new-a.pdf", "new-b.pdf"])
        self.assertEqual(set(rows), {"old.pdf", "undated.pdf", "new-a.pdf", "later.pdf", "new-b.pdf"})
        self.assertEqual(rows["new-b.pdf"], "2026-11-14")
        self.assertIsNone(rows["undated.pdf"])
        self.assertEqual(pw.stats(config), {"in_window": 2, "newly_found": 0, "unknown_date": 1,
                                            "out_of_window": 2, "admitted": 2, "skipped_download": 3})

    def test_in_window_documents_are_prioritized_and_skips_do_not_consume_quota(self):
        downloads, rows = self._crawl(self._config(), max_docs=1)
        # new-a.pdf is third on the page but first to be archived.
        self.assertEqual(downloads, ["new-a.pdf"])
        self.assertIn("new-a.pdf", rows)

    def test_unknown_dates_downloaded_only_when_requested(self):
        downloads, _ = self._crawl(self._config(include_unknown=True))
        self.assertEqual(downloads, ["new-a.pdf", "new-b.pdf", "undated.pdf"])

    def test_malformed_window_fails_closed(self):
        config = self._config(include_unknown="yes")
        with self.assertRaises(ValueError):
            pw.window_from_config(config)


if __name__ == "__main__":
    unittest.main()

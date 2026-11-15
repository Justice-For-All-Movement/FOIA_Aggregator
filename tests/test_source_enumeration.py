"""FC-01/FC-02: source inventory and persistent pagination cursors (fixtures only)."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from foia_archive.enumeration import (
    Page, PaginationError, coverage_report, date_windows, enumerate_source,
    enumerate_windows, require_same_host,
)
from foia_archive.scraper_core import SourceAccessBlocked
from foia_archive.source_inventory import (
    build_source_inventory, normalize_url, source_id_for,
)
from foia_archive.storage import (
    MIGRATIONS, get_connection, init_db, record_reading_room_crawl_failure,
    record_reading_room_crawl_success, upsert_reading_room,
)

FIXTURES = Path(__file__).parent / "fixtures" / "enumeration"


def load_listing():
    return json.loads((FIXTURES / "paged_listing.json").read_text())


class FixtureSite:
    """Serves the recorded listing by page number and counts requests."""

    def __init__(self, fail_on=None, block_on=None):
        self.listing = load_listing()
        self.calls = []
        self.fail_on, self.block_on = fail_on, block_on

    def __call__(self, cursor):
        page = cursor["page"]
        self.calls.append(page)
        if page == self.block_on:
            raise SourceAccessBlocked("HTTP 403 after compatibility retry")
        if page == self.fail_on:
            self.fail_on = None  # transient: next attempt succeeds
            raise ConnectionError("reset")
        record = self.listing["pages"][str(page)]
        nxt = record["next"]
        return Page([{"key": url} for url in record["items"]],
                    {"page": nxt} if nxt else None, self.listing["total_hint"])


class EnumerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "archive.db"
        init_db(self.db, Path(self.temp.name) / "files")
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)

    def run_source(self, site, **kwargs):
        return enumerate_source(self.conn, "src_fixture", site,
                                initial_cursor={"page": 1}, strategy="page_number", **kwargs)

    def test_migration_8_creates_cursor_tables(self):
        self.assertIn((8, "source enumeration cursors"), [(v, n) for v, n, _ in MIGRATIONS])
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"source_cursors", "source_enumeration_items"} <= tables)

    def test_full_traversal_dedupes_and_matches_declared_total(self):
        result = self.run_source(FixtureSite())
        self.assertEqual(result.state, "EXHAUSTED")
        self.assertEqual((result.pages_fetched, result.items_seen, result.new_items), (3, 8, 7))
        report = coverage_report(self.conn)[0]
        self.assertTrue(report["fully_enumerated"])
        self.assertEqual(report["unique_items"], 7)
        self.assertFalse(report["declared_total_mismatch"])

    def test_page_budget_resumes_from_persisted_cursor(self):
        site = FixtureSite()
        first = self.run_source(site, max_pages=1)
        self.assertEqual((first.state, first.reason), ("IN_PROGRESS", "page_budget_reached"))
        # Simulate a process restart with a fresh connection.
        self.conn.close()
        self.conn = get_connection(self.db)
        second = self.run_source(site, max_pages=10)
        self.assertEqual(second.state, "EXHAUSTED")
        self.assertEqual(site.calls, [1, 2, 3])  # page 1 never re-read

    def test_transient_failure_preserves_cursor(self):
        site = FixtureSite(fail_on=2)
        self.assertEqual(self.run_source(site).state, "FAILED")
        self.assertEqual(self.run_source(site).state, "EXHAUSTED")
        self.assertEqual(site.calls, [1, 2, 2, 3])

    def test_access_blocked_is_recorded_and_not_retried(self):
        site = FixtureSite(block_on=2)
        result = self.run_source(site)
        self.assertEqual(result.state, "BLOCKED")
        self.assertIn("403", result.reason)
        again = self.run_source(site)
        self.assertEqual(again.state, "BLOCKED")
        self.assertEqual(site.calls, [1, 2])

    def test_pagination_cycle_fails_closed(self):
        def looping(cursor):
            return Page([{"key": f"k{cursor['page']}"}], {"page": 1 if cursor["page"] == 2 else 2})
        result = self.run_source(looping)
        self.assertEqual((result.state, result.reason), ("FAILED", "pagination_cycle"))

    def test_item_without_stable_key_fails_without_partial_write(self):
        result = self.run_source(lambda cursor: Page([{"title": "no key"}], None))
        self.assertEqual((result.state, result.reason), ("FAILED", "item_without_stable_key"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_enumeration_items").fetchone()[0], 0)

    def test_exhausted_source_is_not_silently_recrawled(self):
        site = FixtureSite()
        self.run_source(site)
        self.assertEqual(self.run_source(site).pages_fetched, 0)
        refreshed = self.run_source(site, restart_exhausted=True)
        self.assertEqual((refreshed.state, refreshed.new_items), ("EXHAUSTED", 0))

    def test_declared_total_mismatch_is_flagged(self):
        enumerate_source(self.conn, "src_short", lambda c: Page([{"key": "only"}], None, total_hint=40),
                         initial_cursor={"page": 1}, strategy="page_number")
        report = {r["source_id"]: r for r in coverage_report(self.conn)}
        self.assertTrue(report["src_short"]["declared_total_mismatch"])

    def test_date_windows_cover_range_without_gaps(self):
        windows = date_windows(date(2019, 11, 15), date(2020, 6, 30), 3)
        self.assertEqual([w["key"] for w in windows], [
            "2019-11-15..2020-01-31", "2020-02-01..2020-04-30", "2020-05-01..2020-06-30"])
        with self.assertRaises(ValueError):
            date_windows(date(2020, 1, 1), date(2019, 1, 1), 1)

    def test_windowed_traversal_dedupes_across_windows_and_stops_on_block(self):
        windows = date_windows(date(2020, 1, 1), date(2020, 12, 31), 6)
        def fetch(window, cursor):
            return Page([{"key": "shared"}, {"key": window["key"]}], None)
        results = enumerate_windows(self.conn, "src_w", windows, fetch,
                                    initial_cursor={"page": 1}, strategy="date_window")
        self.assertEqual([r.new_items for r in results], [2, 1])
        def blocked(window, cursor):
            raise SourceAccessBlocked("CAPTCHA challenge")
        results = enumerate_windows(self.conn, "src_b", windows, blocked,
                                    initial_cursor={"page": 1}, strategy="date_window")
        self.assertEqual([r.state for r in results], ["BLOCKED"])

    def test_next_links_must_stay_on_source_host(self):
        base = "https://foia.example.gov/reading-room/"
        self.assertEqual(require_same_host(base, base + "?page=2"), base + "?page=2")
        for bad in ("https://evil.example/page2", "http://foia.example.gov/p2",
                    "https://user:pw@foia.example.gov/", "javascript:alert(1)", "/relative"):
            with self.subTest(bad=bad), self.assertRaises(PaginationError):
                require_same_host(base, bad)

    def test_unbounded_page_budget_rejected(self):
        for bad in (0, -1, True, 10**9):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_source(FixtureSite(), max_pages=bad)


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "archive.db"
        init_db(self.db, Path(self.temp.name) / "files")
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)
        self.census = {
            "components": [
                {"component_id": "c1", "historical_note": None},
                {"component_id": "c-old", "historical_note": "Merged in 2003"},
            ],
            "source_instances": [
                {"agency_id": "a1", "agency_name": "Example Dept", "component_id": "c1",
                 "component_name": "Office A", "source_type": "reading_room",
                 "url": "https://FOIA.example.gov/reading-room/"},
                {"agency_id": "a1", "agency_name": "Example Dept", "component_id": "c-old",
                 "component_name": "Legacy Office", "source_type": "reading_room",
                 "url": "https://legacy.example.gov/foia"},
            ],
        }

    def test_stable_ids_and_normalization(self):
        self.assertEqual(normalize_url("HTTPS://Foia.Example.gov:443/x#frag"), "https://foia.example.gov/x")
        self.assertEqual(source_id_for("https://foia.example.gov/reading-room/"),
                         source_id_for("https://FOIA.example.gov/reading-room/#top"))

    def test_inventory_merges_official_ic_and_third_party_with_provenance(self):
        inventory = build_source_inventory(self.census)
        summary = inventory["summary"]
        self.assertEqual(summary["ic_elements_expected"], 18)
        self.assertEqual(summary["ic_elements_with_source"], 18)
        self.assertEqual(summary["ic_elements_missing"], [])
        self.assertGreaterEqual(summary["third_party_sources"], 1)
        by_url = {s["url"]: s for s in inventory["sources"]}
        muckrock = by_url["https://www.muckrock.com/api_v1/foia/"]
        self.assertEqual((muckrock["provenance"], muckrock["publisher"]), ("third_party", "MuckRock"))
        self.assertEqual(muckrock["access_status"], "UNVERIFIED")
        self.assertEqual(by_url["https://legacy.example.gov/foia"]["access_status"], "HISTORICAL")
        # Unmeasured counts are None, never an assumed zero.
        self.assertIsNone(by_url["https://foia.example.gov/reading-room/"]["discovered"])
        # Deterministic output for reproducible audits.
        self.assertEqual(json.dumps(inventory, sort_keys=True),
                         json.dumps(build_source_inventory(self.census), sort_keys=True))

    def test_database_counts_access_and_enumeration_state(self):
        ok = upsert_reading_room(self.conn, "https://foia.example.gov/reading-room/", "A", "agency", None, None)
        blocked = upsert_reading_room(self.conn, "https://blocked.example.gov/rr", "B", "agency", None, None)
        stray = upsert_reading_room(self.conn, "https://stray.example.gov/rr", "C", "agency", None, None)
        record_reading_room_crawl_success(self.conn, ok, "2026-11-01T00:00:00+00:00")
        record_reading_room_crawl_failure(self.conn, blocked, "2026-11-02T00:00:00+00:00",
                                          "SourceAccessBlocked: HTTP 403 after compatibility retry")
        for i, status in enumerate(("downloaded", "downloaded", "discovered")):
            self.conn.execute(
                "INSERT INTO documents(url, reading_room_id, download_status) VALUES(?,?,?)",
                (f"https://foia.example.gov/d{i}.pdf", ok, status))
            doc_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.execute("INSERT INTO document_sources(document_id, reading_room_id) VALUES(?,?)", (doc_id, ok))
        self.conn.execute(
            "INSERT INTO document_text(document_id, body, extraction_status, character_count, truncated) "
            "VALUES(1,'x','indexed',1,0)")
        self.conn.commit()
        enumerate_source(self.conn, source_id_for("https://foia.example.gov/reading-room/"),
                         FixtureSite(), initial_cursor={"page": 1}, strategy="page_number")
        census = {"source_instances": self.census["source_instances"] + [
            {"url": "https://blocked.example.gov/rr", "source_type": "reading_room"}]}
        inventory = build_source_inventory(census, conn=self.conn)
        by_url = {s["url"]: s for s in inventory["sources"]}
        main = by_url["https://foia.example.gov/reading-room/"]
        self.assertEqual((main["discovered"], main["archived"], main["indexed"], main["remote_only"]), (3, 2, 1, 1))
        self.assertEqual(main["access_status"], "ACTIVE")
        self.assertEqual(main["enumeration_state"], "EXHAUSTED")
        self.assertEqual(by_url["https://blocked.example.gov/rr"]["access_status"], "BLOCKED")
        self.assertEqual(inventory["database_sources_not_in_inventory"],
                         [source_id_for("https://stray.example.gov/rr")])
        self.assertEqual(inventory["summary"]["remote_only"], 1)


if __name__ == "__main__":
    unittest.main()

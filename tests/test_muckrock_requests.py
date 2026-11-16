"""FC-04 request-listing mode against recorded-shape API v2 fixtures (no network)."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests
from typer.testing import CliRunner

from foia_archive.muckrock_requests import (
    REQUESTS_URL,
    build_list_url,
    list_requests,
    parse_request,
    query_requests,
)
from foia_archive.polite_http import fetch_text
from foia_archive.scraper_core import HostRateLimiter, SourceAccessBlocked
from foia_archive.storage import get_connection, init_db
from foia_archive.utils import Config
from tests.test_export_contract import asgi_request
from ui import server

FIXTURES = Path(__file__).parent / "fixtures" / "muckrock"
PAGE1 = json.loads((FIXTURES / "requests_v2_page_1.json").read_text())
PAGE2 = json.loads((FIXTURES / "requests_v2_page_2.json").read_text())
AGENCY = json.loads((FIXTURES / "agency_v2_77.json").read_text())
START = build_list_url("2026-11-09", "2026-11-16")


class Recorded:
    def __init__(self, block=False):
        self.block, self.calls = block, []

    def __call__(self, url):
        self.calls.append(url)
        if self.block:
            raise SourceAccessBlocked("HTTP 403 from https://www.muckrock.com (Cloudflare)")
        if url == START:
            return PAGE1
        if url == PAGE1["next"]:
            return PAGE2
        if url.endswith("/agencies/77/"):
            return AGENCY
        raise requests.HTTPError(f"HTTP 404 from {url}")


class RequestListingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "archive.db"
        init_db(self.db, Path(tmp.name) / "files")
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)

    def test_list_url_uses_documented_filters(self):
        self.assertTrue(START.startswith(REQUESTS_URL + "?"))
        self.assertIn("datetime_submitted__gte=2026-11-09", START)
        self.assertIn("datetime_submitted__lte=2026-11-16T23%3A59%3A59", START)
        self.assertIn("embargo_status=public", START)
        self.assertIn("datetime_done__gte=2026-11-01", build_list_url("2026-11-01", None, date_field="done"))
        with self.assertRaises(ValueError):
            build_list_url("2026-11-01", None, date_field="filed")

    def test_requester_identity_and_request_text_never_stored(self):
        row = parse_request(PAGE1["results"][0])
        self.assertEqual(row["request_id"], "2001")
        self.assertEqual(row["tags"], "contracts,fixture")
        for forbidden in ("user", "edit_collaborators", "read_collaborators", "requested_docs", "composer"):
            self.assertNotIn(forbidden, row)
        self.assertIsNone(parse_request(PAGE1["results"][1]))  # embargoed

    def test_bounded_listing_stores_public_requests_and_resolves_agency_names(self):
        fetch = Recorded()
        result = list_requests(self.conn, fetch, since="2026-11-09", until="2026-11-16")
        self.assertEqual(result["state"], "EXHAUSTED")
        self.assertEqual((result["requests_seen"], result["requests_new"], result["embargoed_skipped"]), (2, 2, 1))
        self.assertEqual(result["agency_lookups"], 1)  # cached across pages
        listing = query_requests(self.conn, since="2026-11-09", until="2026-11-16")
        self.assertEqual([r["request_id"] for r in listing["requests"]], ["2003", "2001"])
        self.assertEqual(listing["requests"][0]["agency_name"], "Example Department of Fixtures")
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(third_party_requests)")}
        self.assertFalse(columns & {"user", "username", "requester", "requested_docs", "email"})
        # Re-listing the same window refreshes rather than duplicating.
        again = list_requests(self.conn, Recorded(), since="2026-11-09", until="2026-11-16")
        self.assertEqual((again["requests_seen"], again["requests_new"]), (2, 0))
        self.assertEqual(query_requests(self.conn, since="2026-11-15", until=None, date_field="done")["total"], 1)

    def test_denial_is_recorded_blocked_not_evaded(self):
        fetch = Recorded(block=True)
        result = list_requests(self.conn, fetch, since="2026-11-09", until=None)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(len(fetch.calls), 1)

    def test_foreign_next_link_is_rejected(self):
        bad = dict(PAGE1, next="https://evil.example/api_v2/requests/?cursor=x")
        result = list_requests(self.conn, lambda url: bad if "requests" in url else AGENCY,
                               since="2026-11-09", until=None)
        self.assertEqual(result["state"], "FAILED")

    def test_query_tolerates_snapshot_without_table(self):
        self.conn.execute("DROP TABLE third_party_requests")
        self.assertEqual(query_requests(self.conn, since="2026-11-09", until=None)["available"], False)


class TokenAndCliTests(unittest.TestCase):
    def test_cli_disabled_without_token_makes_no_request(self):
        import main
        with patch.dict(os.environ, {"MUCKROCK_API_TOKEN": ""}), \
                patch("foia_archive.polite_http.fetch_json") as fetch:
            result = CliRunner().invoke(main.app, ["muckrock-requests", "--since-days", "7"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("disabled", result.output)
        fetch.assert_not_called()

    def test_cli_requires_a_window(self):
        import main
        result = CliRunner().invoke(main.app, ["muckrock-requests"])
        self.assertNotEqual(result.exit_code, 0)

    def test_token_is_never_sent_through_a_redirect(self):
        class Redirect:
            status_code, headers = 302, {"Location": "https://elsewhere.example/steal"}

            def close(self):
                pass

        seen = []

        def fake_get(url, **kwargs):
            seen.append((url, kwargs["headers"].get("Authorization")))
            return Redirect()

        cfg = Config({"crawler": {"user_agent": "Test/1.0"}})
        with patch("foia_archive.scraper_core._validate_public_destination"), \
                patch("foia_archive.scraper_core.requests.get", side_effect=fake_get):
            with self.assertRaises(requests.TooManyRedirects):
                fetch_text("https://www.muckrock.com/api_v2/requests/", cfg, HostRateLimiter(0), token="secret")
        self.assertEqual([u for u, _ in seen], ["https://www.muckrock.com/api_v2/requests/"])


class RequestsWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "archive.db"
        init_db(self.db, Path(tmp.name) / "files")
        conn = get_connection(self.db)
        today = datetime.now(timezone.utc).date()
        page = json.loads(json.dumps(PAGE2))
        page["results"][0]["datetime_submitted"] = (today - timedelta(days=2)).isoformat() + "T08:00:00"
        list_requests(conn, lambda url: page if "requests" in url else AGENCY, since=None,
                      until=today.isoformat())
        conn.close()
        patcher = patch("ui.server.get_db", side_effect=lambda: get_connection(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._EXPORT_RATE.clear()

    def get(self, path, query=b""):
        return asyncio.run(asgi_request(server.app, path, query))

    def test_requests_page_and_api(self):
        status, _, body = self.get("/requests", b"days=7")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("Fixture: inspection reports 2025", html)
        self.assertIn("requester identities are not stored", html)
        status, _, body = self.get("/api/v1/requests", b"days=7")
        data = json.loads(body)
        self.assertEqual((status, data["total"], data["publisher"]), (200, 1, "MuckRock"))
        self.assertNotIn("user", data["requests"][0])
        self.assertEqual(self.get("/api/v1/requests", b"date_field=filed")[0], 400)
        self.assertEqual(json.loads(self.get("/api/v1/requests", b"days=1")[2])["total"], 0)


if __name__ == "__main__":
    unittest.main()

"""Additive export extensions: document text, request-log rows, log coverage, producer."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from foia_archive.export_contract import ExportCursorError, export_manifest
from foia_archive.export_extensions import (
    COVERAGE_VERSION, LOG_ROW_VERSION, TEXT_VERSION, build_log_coverage, export_log_rows_page,
    export_text_page, load_extension_schema, log_coverage_files, write_dataset_snapshot,
    write_log_coverage,
)
from foia_archive.producer import producer_info
from foia_archive.request_logs import normalize_request_log, store_request_log
from foia_archive.storage import get_connection
from tests.test_export_contract import asgi_request, build_fixture_db, validate
from ui import server

LOG_FIXTURE = Path(__file__).parent / "fixtures" / "request_logs" / "agency_log_fy2024.csv"
SHA = "0123456789abcdef0123456789abcdef01234567"
FIXED_ENV = {"RENDER_GIT_COMMIT": SHA}

# Field sets Send_FOIA_Log_Requests (aggregator_contract.py) requires exactly.
SFL_MANIFEST_FIELDS = {"contract", "contract_version", "dataset", "snapshot_id", "producer", "observed_at",
                       "fixture_mode", "fc14_review", "records"}
SFL_RECORD_FIELDS = {"coverage_id", "jurisdiction", "agency_abbr", "component_abbr", "record_class",
                     "period_start", "period_end", "availability", "artifact_sha256", "source_url",
                     "retrieved_at", "missing_fields", "date_gaps", "synthetic"}


def full_year_log() -> bytes:
    lines = ["Request Number,Date Received,Date Closed,Subject,Final Disposition,Component,Requester"]
    day = date(2022, 1, 3)
    n = 0
    while day.year == 2022:
        n += 1
        lines.append(f"FY-{n:04d},{day.isoformat()},{(day + timedelta(days=9)).isoformat()},"
                     f"Synthetic subject {n},Full Grant,OIP,Private Person {n}")
        day += timedelta(days=14)
    return ("\n".join(lines) + "\n").encode()


class ExtensionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = build_fixture_db(self.root)
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)
        store_request_log(self.conn, normalize_request_log(
            LOG_FIXTURE.read_bytes(), source_url="https://foia.example.gov/logs/fy2024.csv", agency="DOJ"))
        store_request_log(self.conn, normalize_request_log(
            full_year_log(), source_url="https://foia.example.gov/logs/2022.csv", agency="DOJ"))


class ProducerTests(unittest.TestCase):
    def test_env_precedence_and_validation(self):
        self.assertEqual(producer_info({"FOIA_BUILD_COMMIT": SHA, "RENDER_GIT_COMMIT": "f" * 40})["commit"], SHA)
        self.assertEqual(producer_info({"RENDER_GIT_COMMIT": SHA.upper()})["commit_source"], "RENDER_GIT_COMMIT")
        unpinned = producer_info({"RENDER_GIT_COMMIT": "abc123"}, use_git=False)
        self.assertEqual((unpinned["commit"], unpinned["commit_source"]), (None, None))

    def test_fc13_manifest_names_producer_without_breaking_v1(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = get_connection(build_fixture_db(Path(temp)))
            try:
                manifest = export_manifest(conn, env=FIXED_ENV)
            finally:
                conn.close()
        self.assertEqual(manifest["schema_version"], "foia-catalog-export/1")
        self.assertEqual(manifest["contract_revision"], "1.1")
        self.assertEqual(manifest["producer"]["commit"], SHA)

    def test_fc13_record_schema_is_unchanged(self):
        # Downstream consumers pin this file's SHA-256 (FOIA_Aggregator ca822fa).
        raw = (Path(__file__).resolve().parents[1] / "schemas" / "catalog-record.v1.schema.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "1e8abc956b31db20d4855b2c73fb785696f6f0c28c437b91c1fc83c9bd039f34")


class DocumentTextTests(ExtensionFixture):
    def test_text_records_validate_and_hash(self):
        page = export_text_page(self.conn)
        self.assertEqual(page["schema_version"], TEXT_VERSION)
        schema = load_extension_schema("document-text.v1")
        self.assertEqual(len(page["records"]), 1)
        record = page["records"][0]
        validate(record, schema)
        self.assertEqual(record["text_sha256"], hashlib.sha256(record["text"].encode()).hexdigest())
        self.assertEqual(record["source_document"], {"preservation_status": "archived", "sha256": "a" * 64})
        self.assertEqual(record["extraction"], {"status": "indexed", "method": "native_text"})
        self.assertNotIn("key-a.pdf", json.dumps(page))

    def test_pages_are_bounded_and_snapshot_bound(self):
        for doc_id in (2, 3):
            self.conn.execute("INSERT INTO document_text(document_id,body,extraction_status,extraction_method,"
                              "character_count,truncated) VALUES(?,?,'indexed','ocr',3,0)", (doc_id, f"t{doc_id}"))
        self.conn.commit()
        first = export_text_page(self.conn, limit=2)
        second = export_text_page(self.conn, cursor=first["next_cursor"], limit=2)
        self.assertEqual([len(first["records"]), len(second["records"])], [2, 1])
        self.conn.execute("UPDATE document_text SET body='changed!' WHERE document_id=2")
        self.conn.commit()
        with self.assertRaises(ExportCursorError):
            export_text_page(self.conn, cursor=first["next_cursor"])
        with patch("foia_archive.export_extensions.TEXT_PAGE_BYTE_BUDGET", 1):
            budgeted = export_text_page(self.conn, limit=3)
        self.assertEqual(len(budgeted["records"]), 1)
        self.assertIsNotNone(budgeted["next_cursor"])


class RequestLogRowTests(ExtensionFixture):
    def test_rows_validate_and_exclude_requester_data(self):
        page = export_log_rows_page(self.conn, limit=1000)
        self.assertEqual(page["schema_version"], LOG_ROW_VERSION)
        schema = load_extension_schema("request-log-row.v1")
        for record in page["records"]:
            validate(record, schema)
        text = json.dumps(page)
        for private in ("Jane Q. Public", "John Doe", "Private Person", "Example News", "requester_name"):
            self.assertNotIn(private, text)
        self.assertEqual(len(page["records"]), 4 + 26)

    def test_cursor_pagination_covers_every_row_once(self):
        seen, cursor = [], None
        while True:
            page = export_log_rows_page(self.conn, cursor=cursor, limit=7)
            seen += [r["row_id"] for r in page["records"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 30)

    def test_dataset_snapshot_manifest_names_producer(self):
        manifest = write_dataset_snapshot(self.conn, self.root / "rows", "request-log-rows", env=FIXED_ENV)
        self.assertEqual((manifest["counts"]["records"], manifest["producer"]["commit"]), (30, SHA))
        self.assertIn("requester_name", manifest["excluded_fields"])
        body = (self.root / "rows" / manifest["files"][0]["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), manifest["files"][0]["sha256"])


class LogCoverageTests(ExtensionFixture):
    def test_coverage_is_sfl_shape_and_schema_valid(self):
        manifest_raw, body = log_coverage_files(self.conn, env=FIXED_ENV,
                                                now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        manifest = json.loads(manifest_raw)
        self.assertEqual(set(manifest), SFL_MANIFEST_FIELDS)
        validate(manifest, load_extension_schema("log-coverage-manifest.v1"))
        self.assertEqual((manifest["contract_version"], manifest["producer"]),
                         (COVERAGE_VERSION, {"name": "FOIA_Aggregator", "commit": SHA}))
        self.assertEqual(manifest["fc14_review"], {"status": "absent", "reference": None})
        self.assertEqual(manifest["records"]["sha256"], hashlib.sha256(body).hexdigest())
        records = [json.loads(line) for line in body.decode().splitlines()]
        self.assertEqual(manifest["records"]["count"], len(records))
        schema = load_extension_schema("log-coverage-record.v1")
        for record in records:
            self.assertEqual(set(record), SFL_RECORD_FIELDS)
            validate(record, schema)
            for start, end in record["date_gaps"]:
                self.assertTrue(record["period_start"] <= start <= end <= record["period_end"])

    def test_availability_is_conservative(self):
        records = build_log_coverage(self.conn, today=date(2026, 11, 15))
        by_period = {(r["agency_abbr"], r["component_abbr"], r["period_start"][:4]): r for r in records}
        full = by_period[("DOJ", "OIP", "2022")]
        self.assertEqual((full["availability"], full["date_gaps"], full["missing_fields"]), ("full", [], []))
        partial = by_period[("DOJ", "unspecified", "2023")]
        self.assertEqual(partial["availability"], "partial")
        self.assertTrue(partial["date_gaps"])
        # The same log in an unfinished year is never "full".
        self.assertEqual(build_log_coverage(self.conn, today=date(2022, 6, 1))[0]["availability"], "partial")

    def test_fixture_mode_marks_synthetic_producer(self):
        manifest = write_log_coverage(self.conn, self.root / "cov", fixture_mode=True)
        self.assertEqual(manifest["producer"], {"name": "synthetic-fixture", "commit": "synthetic"})
        rows = (self.root / "cov" / "coverage.jsonl").read_text().splitlines()
        self.assertTrue(all(json.loads(r)["synthetic"] for r in rows))

    def test_unpinned_build_reports_null_commit(self):
        with patch("foia_archive.export_extensions.producer_info",
                   return_value={"name": "FOIA_Aggregator", "commit": None}):
            manifest = json.loads(log_coverage_files(self.conn)[0])
        self.assertIsNone(manifest["producer"]["commit"])


class ExtensionApiTests(ExtensionFixture):
    def setUp(self):
        super().setUp()
        patcher = patch("ui.server.get_db", side_effect=lambda: get_connection(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._EXPORT_RATE.clear()

    def get(self, path, query=b""):
        return asyncio.run(asgi_request(server.app, path, query))

    def test_endpoints(self):
        for name in ("document-text.v1", "request-log-row.v1", "log-coverage-record.v1", "log-coverage-manifest.v1"):
            self.assertEqual(self.get(f"/api/v1/export/schemas/{name}")[0], 200)
        self.assertEqual(self.get("/api/v1/export/schemas/../../etc")[0], 404)
        status, _, body = self.get("/api/v1/export/document-text")
        self.assertEqual((status, len(json.loads(body)["records"])), (200, 1))
        status, _, body = self.get("/api/v1/export/request-log-rows", b"limit=5")
        self.assertEqual((status, len(json.loads(body)["records"])), (200, 5))
        self.assertEqual(self.get("/api/v1/export/request-log-rows", b"limit=5000")[0], 400)
        status, _, manifest = self.get("/api/v1/export/log-coverage/manifest.json")
        status2, _, body = self.get("/api/v1/export/log-coverage/coverage.jsonl")
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(json.loads(manifest)["records"]["sha256"], hashlib.sha256(body).hexdigest())


if __name__ == "__main__":
    unittest.main()

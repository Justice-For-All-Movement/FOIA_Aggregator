"""FC-03: request/release log normalization against synthetic fixtures."""
import io
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from foia_archive.request_logs import (
    RequestLogError, map_columns, normalize_request_log, parse_date,
    request_log_summary, store_request_log,
)
from foia_archive.storage import MIGRATIONS, get_connection, init_db

FIXTURE = Path(__file__).parent / "fixtures" / "request_logs" / "agency_log_fy2024.csv"
URL = "https://foia.example.gov/logs/fy2024.csv"


class RequestLogNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.raw = FIXTURE.read_bytes()
        self.log = normalize_request_log(self.raw, source_url=URL, agency="example-dept")

    def test_header_detection_and_column_confidence(self):
        mapping = self.log.column_mapping
        self.assertEqual(mapping["tracking_number"]["header"], "Request Number")
        self.assertEqual(mapping["tracking_number"]["confidence"], 0.9)
        self.assertEqual(mapping["released_record_reference"]["header"], "Reading Room Link")
        self.assertEqual(mapping["requester_name"]["header"], "Requester")
        self.assertEqual(self.log.file_format, "csv")

    def test_rows_keep_provenance_and_parse_dates_without_guessing(self):
        rows = {r["tracking_number"]: r for r in self.log.rows}
        first = rows["EX-2024-0001"]
        self.assertEqual((first["received_date"], first["closed_date"]), ("2023-10-02", "2023-11-15"))
        self.assertEqual(first["row_number"], 4)
        self.assertEqual(first["raw"]["Request Number"], "EX-2024-0001")
        self.assertEqual(first["released_record_reference"], "https://foia.example.gov/docs/ex-2024-0001.pdf")
        self.assertIn("closed_before_received", rows["EX-2024-0002"]["warnings"])
        bad = rows["EX-2024-0003"]
        self.assertIsNone(bad["received_date"])
        self.assertIn("received_date:unparseable_date", bad["warnings"])
        self.assertNotIn("received_date", bad["field_confidence"])
        self.assertEqual(rows["EX-2024-0005"]["received_date"], "2023-10-12")
        # Cell text is stored verbatim as data, never evaluated.
        self.assertTrue(rows["EX-2024-0005"]["request_description"].startswith("=HYPERLINK"))

    def test_unidentifiable_rows_go_to_failed_queue(self):
        self.assertEqual(len(self.log.rows), 4)
        self.assertEqual([f["reason"] for f in self.log.failures], ["no_tracking_number_or_description"])
        self.assertEqual(self.log.failures[0]["row_number"], 8)

    def test_parse_date_variants(self):
        self.assertEqual(parse_date("2020-02-29"), ("2020-02-29", None))
        self.assertEqual(parse_date(datetime(2021, 5, 1, 9)), ("2021-05-01", None))
        self.assertEqual(parse_date(date(2021, 5, 1)), ("2021-05-01", None))
        self.assertEqual(parse_date(45000), ("2023-03-15", "excel_serial_date"))
        self.assertEqual(parse_date("01/01/1900"), (None, "implausible_date"))
        self.assertEqual(parse_date("soon"), (None, "unparseable_date"))
        self.assertEqual(parse_date(True), (None, "unparseable_date"))

    def test_columns_are_not_double_assigned(self):
        mapping = map_columns(["Tracking Number", "Request Number", "Description", "Subject"])
        fields = [canonical for canonical, _ in mapping.values()]
        self.assertEqual(len(fields), len(set(fields)))
        self.assertEqual(mapping[0], ("tracking_number", 1.0))

    def test_xlsx_log_with_native_dates(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "FY2025"
        sheet.append(["Case Number", "Request Date", "Completion Date", "Description", "Status"])
        sheet.append(["X-1", datetime(2024, 11, 3), datetime(2025, 1, 6), "Training manuals", "Granted"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        log = normalize_request_log(buffer.getvalue(), source_url="https://foia.example.gov/log.xlsx")
        self.assertEqual(log.file_format, "xlsx")
        row = log.rows[0]
        self.assertEqual((row["sheet"], row["row_number"]), ("FY2025", 2))
        self.assertEqual((row["received_date"], row["closed_date"]), ("2024-11-03", "2025-01-06"))

    def test_pdf_logs_are_queued_as_unsupported(self):
        log = normalize_request_log(b"%PDF-1.7 fixture", source_url="https://foia.example.gov/log.pdf")
        self.assertEqual(log.rows, [])
        self.assertEqual(log.failures[0]["reason"], "unsupported_format:pdf")

    def test_rejects_empty_headerless_and_oversized_input(self):
        with self.assertRaises(RequestLogError):
            normalize_request_log(b"", source_url=URL)
        with self.assertRaises(RequestLogError):
            normalize_request_log(b"a,b\n1,2\n", source_url=URL)
        with self.assertRaises(RequestLogError):
            normalize_request_log(b"x" * (50 * 1024 * 1024 + 1), source_url=URL)


class RequestLogStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "archive.db"
        init_db(db, Path(self.temp.name) / "files")
        self.conn = get_connection(db)
        self.addCleanup(self.conn.close)

    def test_migration_9_and_idempotent_storage_separate_from_documents(self):
        self.assertIn(9, [v for v, _, _ in MIGRATIONS])
        log = normalize_request_log(FIXTURE.read_bytes(), source_url=URL, agency="example-dept")
        self.assertTrue(store_request_log(self.conn, log))
        self.assertFalse(store_request_log(self.conn, log))
        summary = request_log_summary(self.conn)
        self.assertEqual(summary, {"logs": 1, "rows": 4, "failed_rows": 1, "distinct_tracked_requests": 4})
        # Log rows are request records, never released documents.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        stored = self.conn.execute(
            "SELECT raw_json, field_confidence_json FROM request_log_rows WHERE tracking_number='EX-2024-0001'"
        ).fetchone()
        self.assertEqual(json.loads(stored["raw_json"])["Date Received"], "10/02/2023")
        self.assertEqual(json.loads(stored["field_confidence_json"])["tracking_number"], 0.9)
        failure = self.conn.execute("SELECT row_number, reason FROM request_log_failures").fetchone()
        self.assertEqual(tuple(failure), (8, "no_tracking_number_or_description"))


if __name__ == "__main__":
    unittest.main()

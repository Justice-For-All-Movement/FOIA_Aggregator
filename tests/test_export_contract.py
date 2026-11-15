"""FC-13: versioned catalog export contract, snapshot replay and API."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from foia_archive.export_contract import (
    MAX_PAGE_SIZE, SCHEMA_VERSION, ExportCursorError, decode_cursor, encode_cursor,
    export_manifest, export_page, load_schema, record_id_for, verify_snapshot,
    write_snapshot,
)
from foia_archive.storage import get_connection, init_db, upsert_reading_room
from ui import server

GOLDEN = Path(__file__).parent / "fixtures" / "export" / "catalog_v1_page.json"


def validate(instance, schema, path="$"):
    """Minimal JSON Schema subset validator (type/required/properties/enum/pattern)."""
    import re
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        mapping = {"object": dict, "array": list, "string": str, "integer": int,
                   "boolean": bool, "null": type(None), "number": (int, float)}
        ok = any(isinstance(instance, mapping[t]) and not (t == "integer" and isinstance(instance, bool))
                 for t in allowed)
        if not ok:
            raise AssertionError(f"{path}: {instance!r} is not {allowed}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise AssertionError(f"{path}: missing {key}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                raise AssertionError(f"{path}: unexpected {sorted(extra)}")
        for key, value in instance.items():
            if key in props:
                validate(value, props[key], f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(instance, str):
        if "enum" in schema and instance not in schema["enum"]:
            raise AssertionError(f"{path}: {instance!r} not in enum")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise AssertionError(f"{path}: {instance!r} does not match pattern")


def build_fixture_db(root: Path) -> Path:
    db = root / "archive.db"
    init_db(db, root / "files")
    conn = get_connection(db)
    conn.execute("INSERT INTO agencies(slug,name) VALUES('example-dept','Example Department')")
    conn.execute("INSERT INTO offices(slug,name,agency_id) VALUES('example-office','Example Office',1)")
    room = upsert_reading_room(conn, "https://foia.example.gov/reading-room/", "RR", "agency", 1, 1)
    second = upsert_reading_room(conn, "https://mirror.example.gov/frequently-requested", "FR", "agency", 1, None)
    rows = [
        ("https://foia.example.gov/docs/a.pdf", "Contract A", "2021-03-04", "downloaded", "a" * 64, 1234,
         "/data/files/secret-local-path.pdf", "documents/aa/key-a.pdf"),
        ("https://foia.example.gov/docs/b.pdf", "Memo B", None, "failed", None, None, None, None),
        ("https://foia.example.gov/docs/c.docx", "Letter C", "2019", "pending", None, None, None, None),
    ]
    for url, title, published, status, sha, size, local, key in rows:
        conn.execute(
            """INSERT INTO documents(url,title,published_date,download_status,sha256,file_size,
                   local_path,storage_key,storage_backend,agency_id,office_id,reading_room_id,
                   discovered_at,downloaded_at,download_error,file_type,mime_type,filename)
               VALUES(?,?,?,?,?,?,?,?, 'b2',1,1,?, '2026-01-01T00:00:00+00:00',?, ?, 'pdf','application/pdf','f.pdf')""",
            (url, title, published, status, sha, size, local, key, room,
             "2026-01-02T00:00:00+00:00" if status == "downloaded" else None,
             "Traceback: internal error" if status == "failed" else None))
    conn.execute("INSERT INTO document_sources(document_id,reading_room_id) VALUES(1,?),(1,?)", (room, second))
    conn.execute("""INSERT INTO document_text(document_id,body,extraction_status,extraction_method,character_count,truncated)
                    VALUES(1,'body','indexed','native_text',4,0)""")
    conn.commit()
    conn.close()
    return db


class ExportContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = build_fixture_db(self.root)
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)

    def test_records_validate_against_published_schema(self):
        schema = load_schema()
        page = export_page(self.conn)
        self.assertEqual(page["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(page["records"]), 3)
        for record in page["records"]:
            validate(record, schema)

    def test_golden_page_is_stable(self):
        page = export_page(self.conn, limit=2)
        self.assertEqual(page, json.loads(GOLDEN.read_text()))

    def test_private_and_internal_fields_never_exported(self):
        text = json.dumps(export_page(self.conn))
        for secret in ("secret-local-path", "documents/aa/key-a.pdf", "Traceback", "storage_key", "local_path"):
            self.assertNotIn(secret, text)

    def test_preservation_distinguishes_archived_from_metadata_only(self):
        records = {r["url"]: r for r in export_page(self.conn)["records"]}
        archived = records["https://foia.example.gov/docs/a.pdf"]
        self.assertEqual(archived["preservation"]["status"], "archived")
        self.assertEqual(archived["preservation"]["sha256"], "a" * 64)
        self.assertEqual(len(archived["sources"]), 2)
        self.assertTrue(archived["text"]["indexed"])
        failed = records["https://foia.example.gov/docs/b.pdf"]
        self.assertEqual((failed["preservation"]["status"], failed["preservation"]["acquisition_status"]),
                         ("metadata_only", "failed"))
        self.assertIsNone(failed["preservation"]["sha256"])
        self.assertEqual(records["https://foia.example.gov/docs/c.docx"]["published_year"], 2019)

    def test_stable_record_ids_survive_surrogate_id_changes(self):
        before = {r["url"]: r["record_id"] for r in export_page(self.conn)["records"]}
        # Simulate a rebuilt/restored database that assigned different rowids.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        with self.conn:
            self.conn.execute("UPDATE documents SET id = id + 100")
            self.conn.execute("UPDATE document_sources SET document_id = document_id + 100")
            self.conn.execute("UPDATE document_text SET document_id = document_id + 100")
        self.conn.execute("PRAGMA foreign_keys=ON")
        after = {r["url"]: r["record_id"] for r in export_page(self.conn)["records"]}
        self.assertEqual(before, after)
        self.assertEqual(before["https://foia.example.gov/docs/a.pdf"],
                         record_id_for("https://FOIA.example.gov/docs/a.pdf#page=2"))

    def test_cursor_pagination_and_snapshot_binding(self):
        first = export_page(self.conn, limit=2)
        second = export_page(self.conn, cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(second["records"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.conn.execute("INSERT INTO documents(url, download_status) VALUES('https://foia.example.gov/new','pending')")
        self.conn.commit()
        with self.assertRaises(ExportCursorError):
            export_page(self.conn, cursor=first["next_cursor"])
        for bad in ("not-base64!", encode_cursor(-1, "x"), "e30", "A" * 500):
            with self.subTest(bad=bad), self.assertRaises(ExportCursorError):
                decode_cursor(bad, "snap")
        for bad_limit in (0, MAX_PAGE_SIZE + 1, True):
            with self.subTest(limit=bad_limit), self.assertRaises(ValueError):
                export_page(self.conn, limit=bad_limit)

    def test_snapshot_replays_with_consistent_counts(self):
        out = self.root / "export"
        manifest = write_snapshot(self.conn, out, page_size=2,
                                  now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(manifest["counts"], {"records": 3, "archived": 1, "metadata_only": 2,
                                              "text_indexed": 1, "agencies": 1, "sources": 2})
        self.assertEqual([f["records"] for f in manifest["files"]], [2, 1])
        self.assertEqual(verify_snapshot(out), (True, []))
        # Same snapshot -> byte-identical files.
        again = write_snapshot(self.conn, self.root / "again", page_size=2,
                               now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(manifest["files"], again["files"])
        (out / "records-00001.jsonl").write_text("{}\n")
        ok, problems = verify_snapshot(out)
        self.assertFalse(ok)
        self.assertTrue(any("sha256" in p for p in problems))


async def asgi_request(app, path, query=b""):
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query,
               "root_path": "", "headers": [], "client": ("203.0.113.9", 1234),
               "server": ("testserver", 80)}, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start["headers"]), body


class ExportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = build_fixture_db(Path(self.temp.name))
        patcher = patch("ui.server.get_db", side_effect=lambda: get_connection(self.db))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._EXPORT_RATE.clear()

    def get(self, path, query=b""):
        return asyncio.run(asgi_request(server.app, path, query))

    def test_schema_manifest_and_paged_records(self):
        status, _, body = self.get("/api/v1/export/schema")
        self.assertEqual((status, json.loads(body)["title"].startswith("FOIA Aggregator")), (200, True))
        status, _, body = self.get("/api/v1/export/manifest")
        manifest = json.loads(body)
        self.assertEqual((status, manifest["counts"]["records"]), (200, 3))
        self.assertIn("archive_connected", manifest["archive"])
        status, _, body = self.get("/api/v1/records", b"limit=2")
        page = json.loads(body)
        self.assertEqual((status, len(page["records"])), (200, 2))
        status, _, body = self.get("/api/v1/records", f"cursor={page['next_cursor']}".encode())
        self.assertEqual((status, len(json.loads(body)["records"])), (200, 1))

    def test_bad_cursor_and_limit_are_client_errors(self):
        self.assertEqual(self.get("/api/v1/records", b"cursor=zzz")[0], 409)
        self.assertEqual(self.get("/api/v1/records", b"limit=100000")[0], 400)

    def test_rate_limit_returns_429_with_retry_after(self):
        with patch.dict("os.environ", {"FOIA_EXPORT_RATE_PER_MINUTE": "2"}):
            self.assertEqual(self.get("/api/v1/export/schema")[0], 200)
            self.assertEqual(self.get("/api/v1/export/schema")[0], 200)
            status, headers, _ = self.get("/api/v1/export/schema")
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)


if __name__ == "__main__":
    unittest.main()

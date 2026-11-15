"""Receiving Send_FOIA_Log_Requests sfl-interchange/1 bundles (fixture from SFL itself)."""
import asyncio
import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.sfl_interchange import (
    BundleRejected, envelope_from_directory, import_staged, receive, verify_envelope,
)
from foia_archive.storage import MIGRATIONS, get_connection, init_db
from ui import server

BUNDLE = Path(__file__).parent / "fixtures" / "sfl_bundle"
FIXTURE_SHA = json.loads((BUNDLE / "FIXTURE.json").read_text())["bundle_sha256"]
TOKEN = "t" * 32


def rebuild(envelope, *, fulfilled=None, artifacts=None):
    """Re-hash a modified envelope so it stays internally consistent."""
    env = copy.deepcopy(envelope)
    if fulfilled is not None:
        env["files"]["fulfilled_logs.jsonl"] = "".join(
            json.dumps(r, sort_keys=True) + "\n" for r in fulfilled)
    if artifacts is not None:
        env["artifacts"] = {k: base64.b64encode(v).decode() for k, v in artifacts.items()}
    manifest = json.loads(env["manifest_json"])
    for entry in manifest["files"]:
        body = env["files"][entry["path"]].encode()
        entry["sha256"] = hashlib.sha256(body).hexdigest()
        entry["records"] = len([l for l in body.decode().splitlines() if l.strip()])
    env["manifest_json"] = json.dumps(manifest, indent=2, sort_keys=True)
    return env


def xlsx_log() -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Request Number", "Date Received", "Date Closed", "Subject", "Final Disposition"])
    ws.append(["OIP-2020-1", "2020-01-06", "2020-02-01", "Synthetic subject", "Full Grant"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class VerifyAndStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = Path(self.temp.name) / "inbox"
        self.envelope = envelope_from_directory(BUNDLE)

    def test_fixture_bundle_identity_matches_sfl_export(self):
        bundle_sha, manifest, loaded, artifacts = verify_envelope(self.envelope)
        self.assertEqual(bundle_sha, FIXTURE_SHA)
        self.assertEqual(manifest["counts"], {"components": 7, "fulfilled_logs": 1, "requests": 3})
        self.assertEqual(len(artifacts), 1)

    def test_accept_is_idempotent_with_stable_ack(self):
        status, ack = receive(self.envelope, self.inbox, durable=False)
        self.assertEqual((status, ack["status"], ack["staged"], ack["duplicate"]), (201, "accepted", True, False))
        self.assertEqual(ack["durability"], "ephemeral")
        again_status, again = receive(self.envelope, self.inbox, durable=False)
        self.assertEqual((again_status, again["duplicate"], again["ack_id"]), (200, True, ack["ack_id"]))
        self.assertTrue((self.inbox / FIXTURE_SHA / "manifest.json").is_file())
        self.assertEqual([p.name for p in self.inbox.iterdir()], [FIXTURE_SHA])  # no staging leftovers

    def test_tampering_is_rejected_with_ack(self):
        env = copy.deepcopy(self.envelope)
        env["files"]["requests.jsonl"] = env["files"]["requests.jsonl"].replace("submitted", "fulfilled")
        status, ack = receive(env, self.inbox, durable=False)
        self.assertEqual((status, ack["status"]), (422, "rejected"))
        self.assertIn("SHA-256", ack["detail"])
        self.assertFalse(self.inbox.exists() and any(self.inbox.iterdir()))

    def test_privacy_allowlist_rejects_requester_fields(self):
        manifest = json.loads(self.envelope["manifest_json"])
        rows = [json.loads(l) for l in self.envelope["files"]["requests.jsonl"].splitlines()]
        rows[0]["requester_email"] = "someone@example.invalid"
        env = copy.deepcopy(self.envelope)
        env["files"]["requests.jsonl"] = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        env = rebuild(env)
        with self.assertRaisesRegex(BundleRejected, "allowlist"):
            verify_envelope(env)
        self.assertEqual(manifest["schema_version"], "sfl-interchange/1")

    def test_artifact_mismatch_and_extra_artifacts_rejected(self):
        env = copy.deepcopy(self.envelope)
        sha = next(iter(env["artifacts"]))
        env["artifacts"][sha] = base64.b64encode(b"other bytes").decode()
        with self.assertRaises(BundleRejected):
            verify_envelope(env)
        env = copy.deepcopy(self.envelope)
        env["artifacts"]["f" * 64] = base64.b64encode(b"x").decode()
        with self.assertRaises(BundleRejected):
            verify_envelope(env)

    def test_malformed_envelope_has_no_ack(self):
        for bad in (None, {}, {"envelope_version": "foia-sfl-ingest/1"}, {"envelope_version": "x", "manifest_json": "{}"}):
            status, body = receive(bad, self.inbox, durable=True)
            self.assertEqual(status, 400)
            self.assertNotIn("ack_id", body)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.inbox = root / "inbox"
        init_db(root / "a.db", root / "files")
        self.conn = get_connection(root / "a.db")
        self.addCleanup(self.conn.close)

    def test_migration_11(self):
        self.assertIn(11, [v for v, _, _ in MIGRATIONS])

    def test_import_fixture_records_unparseable_artifact_and_is_idempotent(self):
        receive(envelope_from_directory(BUNDLE), self.inbox, durable=True)
        result = import_staged(self.conn, self.inbox / FIXTURE_SHA)
        self.assertEqual((result["imported"], result["request_logs"]), (True, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sfl_components").fetchone()[0], 7)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sfl_requests").fetchone()[0], 3)
        detail = self.conn.execute("SELECT import_detail FROM sfl_fulfilled_logs").fetchone()[0]
        self.assertIn("Unreadable XLSX", detail)
        self.assertFalse(import_staged(self.conn, self.inbox / FIXTURE_SHA)["imported"])

    def test_tabular_fulfilled_log_flows_into_fc03(self):
        envelope = envelope_from_directory(BUNDLE)
        data = xlsx_log()
        sha = hashlib.sha256(data).hexdigest()
        rows = [json.loads(l) for l in envelope["files"]["fulfilled_logs.jsonl"].splitlines()]
        rows[0].update(sha256=sha, size=len(data))
        env = rebuild(envelope, fulfilled=rows, artifacts={sha: data})
        status, ack = receive(env, self.inbox, durable=True)
        self.assertEqual(status, 201)
        result = import_staged(self.conn, self.inbox / ack["bundle_sha256"])
        self.assertEqual(result["request_logs"], 1)
        row = self.conn.execute("SELECT l.agency, r.tracking_number, l.source_url FROM request_log_rows r "
                                "JOIN request_logs l USING(log_id)").fetchone()
        self.assertEqual(tuple(row)[:2], ("fed:doj", "OIP-2020-1"))
        self.assertTrue(row[2].startswith("urn:sfl:"))

    def test_renamed_staging_directory_is_refused(self):
        receive(envelope_from_directory(BUNDLE), self.inbox, durable=True)
        moved = self.inbox / ("0" * 64)
        (self.inbox / FIXTURE_SHA).rename(moved)
        with self.assertRaises(BundleRejected):
            import_staged(self.conn, moved)


async def post(app, path, body, headers):
    messages, sent = [], False

    async def receive_():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
               "client": ("203.0.113.5", 1), "server": ("testserver", 80)}, receive_, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.body = json.dumps(envelope_from_directory(BUNDLE)).encode()

    def call(self, env, headers):
        with patch.dict(os.environ, env, clear=False):
            return asyncio.run(post(server.app, "/api/v1/ingest/sfl-bundle", self.body,
                                    {"content-type": "application/json", **headers}))

    def test_disabled_without_token_or_with_weak_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOIA_INGEST_TOKEN", None)
            self.assertEqual(asyncio.run(post(server.app, "/api/v1/ingest/sfl-bundle", self.body, {}))[0], 404)
        self.assertEqual(self.call({"FOIA_INGEST_TOKEN": "short"}, {"authorization": "Bearer short"})[0], 404)

    def test_requires_matching_bearer_token(self):
        env = {"FOIA_INGEST_TOKEN": TOKEN, "FOIA_INGEST_INBOX": str(Path(self.temp.name) / "inbox")}
        self.assertEqual(self.call(env, {})[0], 401)
        self.assertEqual(self.call(env, {"authorization": "Bearer " + "x" * 32})[0], 401)
        self.assertEqual(self.call(env, {"authorization": "Basic " + TOKEN})[0], 401)
        status, body = self.call(env, {"authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 201)
        ack = json.loads(body)
        self.assertEqual((ack["status"], ack["bundle_sha256"]), ("accepted", FIXTURE_SHA))
        self.assertEqual(self.call(env, {"authorization": "Bearer " + TOKEN})[0], 200)

    def test_oversized_body_rejected(self):
        env = {"FOIA_INGEST_TOKEN": TOKEN, "FOIA_INGEST_INBOX": str(Path(self.temp.name) / "inbox")}
        with patch("foia_archive.sfl_interchange.MAX_ENVELOPE_BYTES", 100):
            self.assertEqual(self.call(env, {"authorization": "Bearer " + TOKEN})[0], 413)


if __name__ == "__main__":
    unittest.main()

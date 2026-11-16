"""Zero-budget storage: S3-compatible providers, storage policy, text-only
records, GitHub Release catalog snapshots and Wayback preservation."""
import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from foia_archive import github_snapshot as gs
from foia_archive import wayback
from foia_archive.archive_status import resolve_web_database
from foia_archive.archive_storage import (
    B2ArchiveStorage,
    NoArchiveStorage,
    _storage_backend_name,
    get_archive_storage,
    s3_settings,
)
from foia_archive.scraper_core import DownloadResult, crawl_reading_room
from foia_archive.storage import get_connection, init_db, insert_document, upsert_reading_room
from foia_archive.storage_policy import SharedCounter, StoragePolicy
from foia_archive.utils import Config

ROOT = "https://example.gov/reading-room/"
TODAY = datetime.now(timezone.utc).date()


class S3ProviderTests(unittest.TestCase):
    def test_foia_s3_variables_with_b2_aliases(self):
        env = {"B2_KEY_ID": "b2id", "B2_APPLICATION_KEY": "b2key", "FOIA_S3_BUCKET": "bucket"}
        settings = s3_settings(env)
        self.assertEqual((settings["access_key_id"], settings["secret_access_key"], settings["bucket"]),
                         ("b2id", "b2key", "bucket"))
        env["FOIA_S3_ACCESS_KEY_ID"] = "s3id"
        self.assertEqual(s3_settings(env)["access_key_id"], "s3id")

    def test_backend_aliases(self):
        cfg = Config({"storage": {}})
        for name in ("s3", "r2", "oracle", "B2"):
            self.assertEqual(_storage_backend_name(cfg, name), "b2")
        self.assertEqual(_storage_backend_name(cfg, "none"), "none")
        self.assertIsInstance(get_archive_storage(cfg, "none"), NoArchiveStorage)

    def test_r2_endpoint_needs_no_backblaze_discovery(self):
        env = {"FOIA_S3_ENDPOINT": "https://acct.r2.cloudflarestorage.com", "FOIA_S3_BUCKET": "foia",
               "FOIA_S3_ACCESS_KEY_ID": "id", "FOIA_S3_SECRET_ACCESS_KEY": "secret"}
        with patch.dict(os.environ, env, clear=False), \
                patch("foia_archive.archive_storage._authorize_b2_scope") as authorize, \
                patch("foia_archive.archive_storage.boto3.client") as client:
            backend = get_archive_storage(Config({"storage": {"backend": "r2"}}))
        authorize.assert_not_called()
        self.assertIsInstance(backend, B2ArchiveStorage)
        self.assertEqual((backend.bucket, backend.region), ("foia", "auto"))
        self.assertEqual(client.call_args_list[0].kwargs["endpoint_url"], "https://acct.r2.cloudflarestorage.com")

    def test_oracle_endpoint_uses_path_style(self):
        env = {"FOIA_S3_ENDPOINT": "https://ns.compat.objectstorage.us-ashburn-1.oraclecloud.com",
               "FOIA_S3_BUCKET": "foia", "FOIA_S3_REGION": "us-ashburn-1",
               "FOIA_S3_ACCESS_KEY_ID": "id", "FOIA_S3_SECRET_ACCESS_KEY": "secret"}
        with patch.dict(os.environ, env, clear=False), \
                patch("foia_archive.archive_storage.boto3.client") as client:
            get_archive_storage(Config({"storage": {"backend": "oracle"}}))
        config = client.call_args_list[0].kwargs["config"]
        self.assertEqual(config.s3, {"addressing_style": "path"})

    def test_usage_falls_back_when_versions_api_is_missing(self):
        class Client:
            def list_object_versions(self, **kwargs):
                raise RuntimeError("NotImplemented: ListObjectVersions")

            def list_objects_v2(self, **kwargs):
                return {"Contents": [{"Key": "documents/aa/x.pdf", "Size": 10},
                                     {"Key": "database-backups/a.gz", "Size": 5}]}
        backend = B2ArchiveStorage(bucket="b", region="auto", endpoint_url="https://r2.example",
                                   key_id="i", application_key="k", client=Client())
        usage = backend.usage_summary()
        self.assertEqual((usage.stored_bytes, usage.document_bytes, usage.backup_bytes), (15, 10, 5))

    def test_web_status_counts_s3_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.db"
            cfg = Config({"storage": {"backend": "b2", "db_path": str(db)}})
            _, status = resolve_web_database(cfg, {"FOIA_S3_ACCESS_KEY_ID": "i",
                                                   "FOIA_S3_SECRET_ACCESS_KEY": "k"})
            self.assertEqual(status.mode, "connected")
            init_db(db, Path(tmp) / "f")
            path, status = resolve_web_database(cfg, {"FOIA_ARCHIVE_BOOTSTRAP_RESULT": "github_snapshot"})
            self.assertEqual((path, status.mode, status.reason), (db, "snapshot", "catalog_snapshot"))
            self.assertFalse(status.archive_connected)


class PolicyTests(unittest.TestCase):
    def test_tiers(self):
        policy = StoragePolicy(budget_bytes=1000, small_file_bytes=100)
        self.assertTrue(policy.decide(400, 0, recent=False, at_risk=False).keep)
        self.assertFalse(policy.decide(300, 400, recent=False, at_risk=False).keep)   # 70%, large, old
        self.assertTrue(policy.decide(300, 400, recent=True, at_risk=False).keep)
        self.assertTrue(policy.decide(50, 600, recent=False, at_risk=False).keep)     # small
        self.assertFalse(policy.decide(50, 850, recent=False, at_risk=False).keep)    # >85%, not prioritised
        self.assertTrue(policy.decide(50, 850, recent=False, at_risk=True).keep)
        self.assertFalse(policy.decide(200, 900, recent=True, at_risk=True).keep)     # over budget
        none = StoragePolicy(budget_bytes=0, keep_originals=False)
        self.assertFalse(none.decide(1, 0, recent=True, at_risk=True).keep)
        self.assertTrue(StoragePolicy(budget_bytes=0).decide(10 ** 12, 0, recent=False, at_risk=False).keep)

    def test_budget_sources_and_recency(self):
        cfg = Config({"storage": {"policy": {"at_risk_hosts": ["fragile.gov"]}}})
        with patch.dict(os.environ, {"FOIA_ORIGINALS_BUDGET_BYTES": "500"}):
            self.assertEqual(StoragePolicy.from_config(cfg, "b2", remote_cap=8_500).budget_bytes, 500)
        with patch.dict(os.environ, {"FOIA_ORIGINALS_BUDGET_BYTES": ""}):
            self.assertEqual(StoragePolicy.from_config(cfg, "b2", remote_cap=8_500).budget_bytes, 8_500)
            self.assertEqual(StoragePolicy.from_config(cfg, "local").budget_bytes, 0)
        policy = StoragePolicy.from_config(cfg, "b2", remote_cap=1)
        self.assertTrue(policy.is_at_risk("https://www.fragile.gov/x.pdf"))
        self.assertTrue(policy.is_recent((TODAY - timedelta(days=3)).isoformat(), None))
        self.assertFalse(policy.is_recent("2020-01-01", None))
        self.assertTrue(policy.is_recent(None, "2026-11-01T00:00:00"))

    def test_shared_counter_survives_deepcopy(self):
        import copy
        counter = SharedCounter(10)
        data = copy.deepcopy({"c": counter})
        self.assertIs(data["c"], counter)
        self.assertTrue(counter.reserve(6))
        self.assertFalse(counter.reserve(6))


class Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self.status_code = status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.encoding = "utf-8"
        self._body = body if isinstance(body, bytes) else body.encode()

    def iter_content(self, chunk_size):
        yield self._body

    def json(self):
        return json.loads(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def close(self):
        pass


class TextOnlyCrawlTests(unittest.TestCase):
    def test_none_backend_keeps_text_and_hash_not_file_and_never_refetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, files = Path(tmp) / "a.db", Path(tmp) / "files"
            init_db(db, files)
            conn = get_connection(db)
            rr = upsert_reading_room(conn, ROOT, "Room", "office", None, None, source_type="reading_room")
            conn.close()
            cfg = Config({"crawler": {"user_agent": "T/1", "max_pages_per_source": 1, "max_depth": 0,
                                      "per_host_delay_seconds": 0},
                          "downloader": {"max_redirects": 1, "max_retries": 0},
                          "storage": {"backend": "none", "db_path": str(db), "files_dir": str(files)}})
            calls = []

            def fake_download(url, hint, config, **kwargs):
                calls.append(url)
                path = files / "staged.txt"
                path.write_text("public release text")
                return DownloadResult(status="downloaded", path=path, sha256="ab" * 32,
                                      mime_type="text/plain", file_size=19)

            with patch("foia_archive.scraper_core._validate_public_destination", return_value=None), \
                    patch("foia_archive.scraper_core.requests.get",
                          return_value=Resp('<a href="r.txt">Release</a>')), \
                    patch("foia_archive.scraper_core.download_document", side_effect=fake_download):
                crawl_reading_room(rr, cfg, dry_run=False, max_docs=5)
                crawl_reading_room(rr, cfg, dry_run=False, max_docs=5)
            conn = get_connection(db)
            row = conn.execute("SELECT d.download_status, d.sha256, d.storage_key, d.storage_backend, "
                               "t.body FROM documents d JOIN document_text t ON t.document_id = d.id").fetchone()
            conn.close()
            self.assertEqual(tuple(row), ("text_only", "ab" * 32, None, "none", "public release text"))
            self.assertFalse((files / "staged.txt").exists())
            self.assertEqual(len(calls), 1)


class SnapshotTests(unittest.TestCase):
    def make_db(self, root, docs):
        db = root / "src.db"
        init_db(db, root / "f")
        conn = get_connection(db)
        for i in range(docs):
            insert_document(conn, url=f"https://e.gov/{i}.pdf", title=f"T{i}", file_type="pdf",
                            filename=f"{i}.pdf", agency_id=None, office_id=None, reading_room_id=None,
                            discovered_at="2026-11-01T00:00:00")
        conn.close()
        return db

    def test_build_verify_restore_and_monotonic_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self.make_db(root, 3)
            manifest = gs.build_snapshot(db, root / "out")
            self.assertEqual(manifest["counts"]["documents"], 3)
            self.assertRegex(manifest["asset"], gs.ASSET_RE)
            with self.assertRaises(gs.SnapshotError):
                gs.build_snapshot(db, root / "out2", previous={"counts": {"documents": 5}})
            blob = (root / "out" / manifest["asset"]).read_bytes()
            self.assertEqual(hashlib.sha256(blob).hexdigest(), manifest["sha256"])

            def fake_get(url, **kwargs):
                if url.endswith("/latest.json"):
                    return Resp(json.dumps(manifest))
                if url.endswith(manifest["asset"]):
                    return Resp(blob)
                return Resp(status=404)

            with patch("foia_archive.github_snapshot.requests.get", side_effect=fake_get):
                restored = gs.restore_latest("owner/repo", root / "live" / "foia.db")
                self.assertEqual(restored["asset"], manifest["asset"])
                with self.assertRaises(gs.SnapshotError):
                    gs.restore_latest("owner/repo", root / "live" / "foia.db")  # never overwrite
            conn = sqlite3.connect(root / "live" / "foia.db")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 3)
            conn.close()

            tampered = dict(manifest, sha256="0" * 64)
            with patch("foia_archive.github_snapshot.requests.get",
                       side_effect=lambda url, **k: Resp(json.dumps(tampered)) if url.endswith(".json")
                       else Resp(blob)):
                with self.assertRaises(gs.SnapshotError):
                    gs.restore_latest("owner/repo", root / "other.db")
            self.assertFalse((root / "other.db").exists())
            with patch("foia_archive.github_snapshot.requests.get", return_value=Resp(status=404)):
                self.assertIsNone(gs.restore_latest("owner/repo", root / "none.db"))

    def test_names_and_prune(self):
        with self.assertRaises(gs.SnapshotError):
            gs.release_url("owner/repo", "../evil")
        with self.assertRaises(gs.SnapshotError):
            gs.release_url("bad repo", "latest.json")
        names = ["latest.json", "foia-catalog-20261101T000000Z.db.gz", "foia-catalog-20261108T000000Z.db.gz",
                 "foia-catalog-20261115T000000Z.db.gz", "foia-catalog-20261116T000000Z.db.gz"]
        self.assertEqual(gs.prune_plan(names, 3), ["foia-catalog-20261101T000000Z.db.gz"])


class FakeWayback:
    authenticated = False

    def __init__(self, existing=None, limit_after=None):
        self.existing_map = existing or {}
        self.saved = []
        self.limit_after = limit_after

    def existing(self, url):
        return self.existing_map.get(url)

    def save(self, url):
        if self.limit_after is not None and len(self.saved) >= self.limit_after:
            raise wayback.RateLimited("429")
        self.saved.append(url)
        return "saved", f"https://web.archive.org/web/20261116000000/{url}", None


class WaybackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "a.db"
        init_db(self.db, Path(tmp.name) / "f")
        conn = get_connection(self.db)
        for name, status in (("a", "downloaded"), ("b", "text_only"), ("c", "pending"), ("d", "downloaded")):
            insert_document(conn, url=f"https://e.gov/{name}.pdf", title=name, file_type="pdf",
                            filename=f"{name}.pdf", agency_id=None, office_id=None, reading_room_id=None,
                            discovered_at=f"2026-11-0{ord(name) - 96}T00:00:00")
            conn.execute("UPDATE documents SET download_status = ? WHERE url = ?", (status, f"https://e.gov/{name}.pdf"))
        conn.commit()
        conn.close()

    def rows(self):
        conn = get_connection(self.db)
        try:
            return {r[0].rsplit("/", 1)[-1]: (r[1], r[2]) for r in conn.execute(
                "SELECT url, wayback_status, wayback_url FROM documents")}
        finally:
            conn.close()

    def test_existing_snapshots_recorded_new_ones_submitted_and_pending_skipped(self):
        client = FakeWayback(existing={"https://e.gov/d.pdf": "https://web.archive.org/web/1/x"})
        conn = get_connection(self.db)
        counts = wayback.preserve(conn, "T/1", client=client, sleep=lambda s: None)
        conn.close()
        rows = self.rows()
        self.assertEqual(counts["existing"], 1)
        self.assertEqual(rows["d.pdf"][0], "existing")
        self.assertEqual(rows["a.pdf"][0], "saved")
        self.assertEqual(rows["b.pdf"][0], "saved")
        self.assertIsNone(rows["c.pdf"][0])   # never fetched: not submitted
        self.assertEqual(len(client.saved), 2)

    def test_rate_limit_stops_the_run(self):
        conn = get_connection(self.db)
        counts = wayback.preserve(conn, "T/1", client=FakeWayback(limit_after=1), sleep=lambda s: None)
        conn.close()
        self.assertTrue(counts["rate_limited"])
        self.assertEqual(sum(1 for s, _ in self.rows().values() if s == "saved"), 1)

    def test_client_parses_anonymous_and_spn2_responses(self):
        class Session:
            def __init__(self, responses):
                self.responses = responses
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(("GET", url))
                return self.responses.pop(0)

            def post(self, url, **kwargs):
                self.calls.append(("POST", url))
                return self.responses.pop(0)

        anon = wayback.WaybackClient("T/1", session=Session([
            Resp(status=302, headers={"Content-Location": "/web/20261116010203/https://e.gov/a.pdf"})]))
        self.assertEqual(anon.save("https://e.gov/a.pdf")[1],
                         "https://web.archive.org/web/20261116010203/https://e.gov/a.pdf")
        spn2 = wayback.WaybackClient("T/1", access_key="k", secret_key="s", sleep=lambda s: None, session=Session([
            Resp(json.dumps({"job_id": "spn2-1"}), headers={"Content-Type": "application/json"}),
            Resp(json.dumps({"status": "success", "timestamp": "20261116000000",
                             "original_url": "https://e.gov/a.pdf"}), headers={"Content-Type": "application/json"})]))
        self.assertEqual(spn2.save("https://e.gov/a.pdf"),
                         ("saved", "https://web.archive.org/web/20261116000000/https://e.gov/a.pdf", "spn2-1"))
        self.assertEqual(spn2.headers["Authorization"], "LOW k:s")
        limited = wayback.WaybackClient("T/1", session=Session([Resp(status=429)]))
        with self.assertRaises(wayback.RateLimited):
            limited.save("https://e.gov/a.pdf")


if __name__ == "__main__":
    unittest.main()

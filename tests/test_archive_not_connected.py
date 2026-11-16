"""The web service must start cleanly and say so when B2 is not connected."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from foia_archive.archive_status import placeholder_path, resolve_web_database
from foia_archive.storage import init_db
from foia_archive.utils import Config

REPO = Path(__file__).resolve().parents[1]


def config_for(db_path, backend="b2"):
    return Config({"storage": {"backend": backend, "db_path": str(db_path),
                               "files_dir": str(Path(db_path).parent / "files")}})


class ResolveWebDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "foia_archive.db"
        self.creds = {"B2_KEY_ID": "fixture-id", "B2_APPLICATION_KEY": "fixture-key"}

    def test_missing_credentials_serves_placeholder_not_configured_path(self):
        path, status = resolve_web_database(
            config_for(self.db), {"FOIA_BOOTSTRAP_DB_FROM_B2": "true"})
        self.assertEqual(path, placeholder_path(self.db))
        self.assertNotEqual(path, self.db)
        self.assertFalse(status.archive_connected)
        self.assertEqual(status.reason, "b2_credentials_missing")
        self.assertTrue(status.serving_placeholder_index)
        self.assertIn("not connected", status.message)

    def test_existing_snapshot_without_credentials_keeps_metadata_search(self):
        init_db(self.db, self.db.parent / "files")
        path, status = resolve_web_database(config_for(self.db), {})
        self.assertEqual(path, self.db)
        self.assertFalse(status.archive_connected)
        self.assertFalse(status.serving_placeholder_index)

    def test_failed_bootstrap_with_credentials_is_reported(self):
        env = {**self.creds, "FOIA_BOOTSTRAP_DB_FROM_B2": "true",
               "FOIA_ARCHIVE_BOOTSTRAP_RESULT": "failed"}
        path, status = resolve_web_database(config_for(self.db), env)
        self.assertEqual(path, placeholder_path(self.db))
        self.assertEqual(status.reason, "snapshot_not_loaded")
        self.assertEqual(status.bootstrap_result, "failed")

    def test_restored_snapshot_is_connected(self):
        init_db(self.db, self.db.parent / "files")
        env = {**self.creds, "FOIA_BOOTSTRAP_DB_FROM_B2": "true",
               "FOIA_ARCHIVE_BOOTSTRAP_RESULT": "restored"}
        path, status = resolve_web_database(config_for(self.db), env)
        self.assertEqual(path, self.db)
        self.assertTrue(status.archive_connected)
        self.assertIsNone(status.message)

    def test_colocated_writer_without_bootstrap_uses_configured_path(self):
        path, status = resolve_web_database(config_for(self.db), self.creds)
        self.assertEqual(path, self.db)
        self.assertEqual(status.mode, "connected")

    def test_local_backend_is_unchanged(self):
        path, status = resolve_web_database(config_for(self.db, "local"), {})
        self.assertEqual((path, status.mode), (self.db, "local"))

    def test_unknown_bootstrap_result_and_secrets_never_reach_public_status(self):
        env = {**self.creds, "FOIA_ARCHIVE_BOOTSTRAP_RESULT": "<script>"}
        _, status = resolve_web_database(config_for(self.db), env)
        public = json.dumps(status.public())
        self.assertIsNone(status.bootstrap_result)
        self.assertNotIn("fixture-key", public)
        self.assertNotIn("fixture-id", public)
        self.assertNotIn(str(self.db), public)


SERVER_PROBE = textwrap.dedent("""
    import asyncio, json, sqlite3, sys
    from pathlib import Path
    sys.path.insert(0, "tests")
    from test_deployment import asgi_get
    from ui import server
    from foia_archive.storage import get_connection, init_db
    out = {}
    out["health"] = json.loads(asyncio.run(asgi_get(server.app, "/healthz"))[1])
    status, body = asyncio.run(asgi_get(server.app, "/"))
    out["search_status"] = status
    out["banner"] = "Archive not connected." in body
    out["configured_exists"] = Path(server.CONFIGURED_DB_PATH).exists()
    # A verified snapshot appearing later (e.g. restored by an operator) is adopted.
    init_db(server.CONFIGURED_DB_PATH, server.FILES_DIR)
    asyncio.run(asgi_get(server.app, "/healthz"))
    out["adopted"] = str(server.DB_PATH) == str(server.CONFIGURED_DB_PATH)
    print("RESULT" + json.dumps(out))
""")


class ServerWithoutB2Tests(unittest.TestCase):
    def run_server_probe(self, extra_env):
        with tempfile.TemporaryDirectory() as temp:
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("B2_", "FOIA_"))}
            env.update({
                "FOIA_STORAGE_BACKEND": "b2",
                "FOIA_BOOTSTRAP_DB_FROM_B2": "true",
                "FOIA_DB_PATH": str(Path(temp) / "foia_archive.db"),
                "FOIA_FILES_DIR": str(Path(temp) / "files"),
                "PYTHONPATH": str(REPO),
                **extra_env,
            })
            completed = subprocess.run(
                [sys.executable, "-c", SERVER_PROBE], cwd=REPO, env=env,
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            line = next(l for l in completed.stdout.splitlines() if l.startswith("RESULT"))
            return json.loads(line[len("RESULT"):])

    def test_starts_with_empty_index_and_reports_archive_not_connected(self):
        result = self.run_server_probe({})
        self.assertEqual(result["health"]["status"], "ok")
        archive = result["health"]["archive"]
        self.assertFalse(archive["archive_connected"])
        self.assertEqual(archive["reason"], "b2_credentials_missing")
        self.assertTrue(archive["serving_placeholder_index"])
        self.assertEqual(result["search_status"], 200)
        self.assertTrue(result["banner"])
        # The configured path was not created, so a later bootstrap can run.
        self.assertFalse(result["configured_exists"])
        self.assertTrue(result["adopted"])


@unittest.skipIf(sys.platform.startswith("win"), "POSIX shell entrypoint")
class EntrypointTests(unittest.TestCase):
    def run_entrypoint(self, env_extra, restore_exit=0, snapshot_creates_db=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        shims = root / "bin"
        shims.mkdir()
        (shims / "chown").write_text("#!/bin/sh\nexit 0\n")
        # gosu USER CMD...: simulate the B2 restore step, otherwise run CMD.
        (shims / "gosu").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            shift
            if [ "$1" = python ] && [ "$3" = bootstrap-github-snapshot ]; then
              {"echo db > \"$FOIA_DB_PATH\"; exit 0" if snapshot_creates_db else "exit 4"}
            fi
            if [ "$1" = python ] && [ "$2" = /app/main.py ]; then exit {restore_exit}; fi
            exec "$@"
        """))
        for shim in shims.iterdir():
            shim.chmod(0o755)
        env = {"PATH": f"{shims}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "FOIA_DB_PATH": str(root / "data" / "foia_archive.db"),
               "FOIA_FILES_DIR": str(root / "data" / "files"),
               "FOIA_BOOTSTRAP_DB_FROM_B2": "true", **env_extra}
        return subprocess.run(
            ["sh", str(REPO / "docker-entrypoint.sh"), "sh", "-c",
             'echo "RESULT=$FOIA_ARCHIVE_BOOTSTRAP_RESULT"'],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_missing_credentials_start_instead_of_crashing(self):
        completed = self.run_entrypoint({})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("RESULT=skipped_no_credentials", completed.stdout)

    def test_failed_restore_starts_not_connected(self):
        completed = self.run_entrypoint(
            {"B2_KEY_ID": "id", "B2_APPLICATION_KEY": "key"}, restore_exit=1)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("RESULT=failed", completed.stdout)

    def test_zero_storage_mode_installs_published_catalog_snapshot(self):
        completed = self.run_entrypoint({}, snapshot_creates_db=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("RESULT=github_snapshot", completed.stdout)
        disabled = self.run_entrypoint({"FOIA_GITHUB_SNAPSHOT_REPO": ""}, snapshot_creates_db=True)
        self.assertIn("RESULT=skipped_no_credentials", disabled.stdout)

    def test_strict_mode_refuses_to_start_without_archive(self):
        completed = self.run_entrypoint({"FOIA_REQUIRE_ARCHIVE": "true"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("RESULT=", completed.stdout)


class ContainerBindingTests(unittest.TestCase):
    def test_dockerfile_binds_all_interfaces_on_platform_port(self):
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
        self.assertIn("--host 0.0.0.0", cmd)
        self.assertIn("--port ${PORT:-8000}", cmd)
        self.assertIn('ENTRYPOINT ["/app/docker-entrypoint.sh"]', dockerfile)

    def test_render_blueprint_declares_unsynced_archive_and_admin_settings(self):
        service = yaml.safe_load((REPO / "render.yaml").read_text())["services"][0]
        env = {item["key"]: item for item in service["envVars"]}
        for key in ("B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET", "B2_REGION",
                    "B2_ENDPOINT_URL", "FOIA_ADMIN_PASSWORD"):
            self.assertIs(env[key]["sync"], False, key)
            self.assertNotIn("value", env[key], key)


if __name__ == "__main__":
    unittest.main()

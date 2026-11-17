"""The crawl restores the freshest catalog, never a stale checkpoint over newer work."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pick", Path(__file__).parent.parent / "scripts" / "pick_newest_catalog.py")
pick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pick)


def make(path, run, docs):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ingestion_runs(started_at TEXT, updated_at TEXT, finished_at TEXT)")
    conn.execute("CREATE TABLE documents(id INTEGER)")
    conn.execute("INSERT INTO ingestion_runs VALUES(?,?,?)", (run, run, run))
    conn.executemany("INSERT INTO documents VALUES(?)", [(i,) for i in range(docs)])
    conn.commit()
    conn.close()


class PickNewestTests(unittest.TestCase):
    def test_newer_snapshot_beats_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new, dest = (Path(tmp, n) for n in ("checkpoint.db", "snapshot.db", "dest.db"))
            make(old, "2026-10-06T08:07:58", 9000)
            make(new, "2026-11-17T03:38:24", 28424)
            self.assertEqual(pick.main(["x", str(dest), str(old), str(new)]), 0)
            self.assertEqual(pick.freshness(dest), ("2026-11-17T03:38:24", 28424))
            self.assertTrue(old.exists())

    def test_missing_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pick.main(["x", str(Path(tmp, "d.db")), str(Path(tmp, "none.db"))]), 4)


if __name__ == "__main__":
    unittest.main()

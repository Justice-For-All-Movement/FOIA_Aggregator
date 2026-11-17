"""Showcase archive: diverse selection, guarded prune, budgeted fill (fakes only)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from foia_archive.showcase import CONFIRM_PHRASE, fill_selection, load_priorities, plan_showcase, prune_to_selection
from foia_archive.storage import get_connection, init_db

PRIORITIES = Path(__file__).parent.parent / "config" / "agency_priority.yaml"


class FakeStorage:
    name = "b2"

    def __init__(self, objects):
        self.objects = dict(objects)
        self.stored = []

    def current_object_manifest(self, prefix=""):
        return {k: v for k, v in self.objects.items() if k.startswith(prefix)}

    def store_file(self, path, *, sha256, filename, mime_type=None, current_usage_bytes=0, known_present=False):
        key = f"documents/{sha256}.pdf"
        self.objects[key] = Path(path).stat().st_size
        self.stored.append(key)
        return SimpleNamespace(backend="b2", key=key, local_path=None)


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        init_db(self.root / "s.db", self.root / "files")
        self.conn = get_connection(self.root / "s.db")
        agencies = [("department-of-justice", "Department of Justice"),
                    ("marine-mammal-commission", "Marine Mammal Commission"),
                    ("department-of-labor", "Department of Labor")]
        for slug, name in agencies:
            self.conn.execute("INSERT INTO agencies(slug,name) VALUES(?,?)", (slug, name))
        ids = {r["slug"]: r["id"] for r in self.conn.execute("SELECT id, slug FROM agencies")}
        n = 0
        for slug, count in (("department-of-labor", 50), ("marine-mammal-commission", 3), ("department-of-justice", 4)):
            for i in range(count):
                n += 1
                stored = slug == "department-of-labor" and i < 10
                self.conn.execute(
                    """INSERT INTO documents(url,filename,title,file_type,agency_id,file_size,download_status,
                           storage_backend,storage_key) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (f"https://x.gov/{slug}/{i}.pdf", f"{i}.pdf", f"doc {i}", "pdf", ids[slug], 1_000_000,
                     "downloaded" if stored else "pending", "b2" if stored else "local",
                     f"documents/{slug}-{i}" if stored else None))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_round_robin_spreads_budget_across_agencies(self):
        plan = plan_showcase(self.conn, rules=load_priorities(PRIORITIES), budget_bytes=9_000_000)
        self.assertEqual(plan["documents"], 9)
        self.assertEqual(plan["agencies_selected"], 3)
        rows = self.conn.execute("SELECT agency_slug, tier FROM showcase_selection ORDER BY rank").fetchall()
        self.assertEqual(rows[0]["agency_slug"], "department-of-justice")  # tier 1 first
        counts = {}
        for r in rows:
            counts[r["agency_slug"]] = counts.get(r["agency_slug"], 0) + 1
        self.assertEqual(counts, {"department-of-justice": 3, "department-of-labor": 3, "marine-mammal-commission": 3})

    def test_prune_requires_phrase_and_keeps_text(self):
        plan_showcase(self.conn, rules=load_priorities(PRIORITIES), budget_bytes=9_000_000)
        storage = FakeStorage({f"documents/department-of-labor-{i}": 1_000_000 for i in range(10)})
        deleted = []
        with self.assertRaises(PermissionError):
            prune_to_selection(self.conn, storage, lambda s, k: deleted.append(k), confirm="yes")
        self.assertEqual(deleted, [])
        result = prune_to_selection(self.conn, storage, lambda s, k: deleted.append(k), confirm=CONFIRM_PHRASE)
        self.assertEqual(result["kept"], 3)  # three already-stored Labor docs were selected
        self.assertEqual(result["deleted"], 7)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE storage_backend='b2'").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 57)

    def test_fill_downloads_selected_within_budget(self):
        plan_showcase(self.conn, rules=load_priorities(PRIORITIES), budget_bytes=9_000_000)
        storage = FakeStorage({f"documents/department-of-labor-{i}": 1_000_000 for i in range(10)})
        skipped = fill_selection(self.conn, SimpleNamespace(data={}), storage, budget_bytes=9_000_000,
                                 download=None, rate_limiter=None, files_dir=self.root)
        self.assertEqual(skipped["stored"], 0)  # 10 MB already stored > 9 MB budget: prune must come first
        prune_to_selection(self.conn, storage, lambda s, k: storage.objects.pop(k), confirm=CONFIRM_PHRASE)
        body = self.root / "f.pdf"

        def download(url, hint, config, rate_limiter=None):
            body.write_bytes(b"%PDF-1.4 test " + url.encode())
            return SimpleNamespace(status="downloaded", path=body, file_size=body.stat().st_size,
                                   sha256=str(abs(hash(url))), mime_type="application/pdf", error=None)

        config = SimpleNamespace(data={})
        result = fill_selection(self.conn, config, storage, budget_bytes=9_000_000, download=download,
                                rate_limiter=None, files_dir=self.root)
        self.assertEqual(result["stored"], 6)  # 3 Labor docs were already stored
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()

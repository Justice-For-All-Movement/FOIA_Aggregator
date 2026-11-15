"""FC-09 metadata-first growth, FC-10 leased work queue, FC-11 migration gate."""
import random
import tempfile
import unittest
from pathlib import Path

from foia_archive.acquisition import (
    enqueue_remote_only, growth_mode, next_acquisitions, queue_summary, record_availability,
)
from foia_archive.export_contract import export_page
from foia_archive.migration_plan import migration_gate, shadow_compare, storage_cost_estimate
from foia_archive.storage import MIGRATIONS, get_connection, init_db
from foia_archive.work_queue import complete, enqueue, fail, heartbeat, lease, stats


class DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "a.db"
        init_db(self.db, Path(self.temp.name) / "f")
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)


class AcquisitionTests(DbCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO agencies(id,slug,name) VALUES(1,'has','Has archive'),(2,'none','Nothing yet')")
        rows = [(1, 1, "downloaded", 100), (2, 1, "pending", 50), (3, 2, "pending", 400),
                (4, 2, "failed", None), (5, 1, "pending", 30)]
        for doc_id, agency, status, size in rows:
            self.conn.execute("INSERT INTO documents(id,url,agency_id,download_status,file_size) VALUES(?,?,?,?,?)",
                              (doc_id, f"https://x.gov/{doc_id}", agency, status, size))
        self.conn.commit()

    def test_migration_13_and_growth_mode(self):
        self.assertIn(13, [v for v, _, _ in MIGRATIONS])
        self.assertEqual(growth_mode(8_499_999_999, 8_500_000_000), "binary")
        self.assertEqual(growth_mode(8_500_000_000, 8_500_000_000), "metadata_only")
        with self.assertRaises(ValueError):
            growth_mode(-1, 10)

    def test_queue_prioritizes_unarchived_agencies_and_respects_budget(self):
        self.assertEqual(enqueue_remote_only(self.conn), 4)
        self.assertEqual(enqueue_remote_only(self.conn), 0)
        plan = next_acquisitions(self.conn, budget_bytes=480)
        self.assertEqual([p["document_id"] for p in plan], [3, 5, 2])  # agency 2 first; unknown-size 4 waits
        self.assertLessEqual(sum(p["size_hint"] or 0 for p in plan), 480)
        self.assertEqual([p["document_id"] for p in next_acquisitions(self.conn, budget_bytes=450)], [3, 5])
        self.assertEqual([p["document_id"] for p in next_acquisitions(self.conn, budget_bytes=0)], [4])

    def test_broken_links_wait_and_queued_records_stay_metadata_only(self):
        enqueue_remote_only(self.conn)
        record_availability(self.conn, 3, "broken")
        self.assertNotIn(3, [p["document_id"] for p in next_acquisitions(self.conn, budget_bytes=10_000)])
        self.assertEqual(queue_summary(self.conn)["broken"]["records"], 1)
        statuses = {r["url"]: r["preservation"]["status"] for r in export_page(self.conn)["records"]}
        self.assertEqual(statuses["https://x.gov/3"], "metadata_only")
        with self.assertRaises(ValueError):
            record_availability(self.conn, 3, "maybe")


class WorkQueueTests(DbCase):
    def test_idempotent_enqueue_and_lease_ownership(self):
        self.assertTrue(enqueue(self.conn, "crawl", "a", host="a.gov", now=0))
        self.assertFalse(enqueue(self.conn, "crawl", "a", host="a.gov", now=0))
        item = lease(self.conn, "w1", now=1)[0]
        self.assertFalse(complete(self.conn, item["id"], "w2", now=2))  # not the holder
        self.assertTrue(heartbeat(self.conn, item["id"], "w1", now=2, lease_seconds=10))
        self.assertTrue(complete(self.conn, item["id"], "w1", now=3))
        self.assertEqual(stats(self.conn)["DONE"], 1)

    def test_crashed_worker_lease_expires_and_stale_completion_refused(self):
        enqueue(self.conn, "crawl", "a", host="a.gov", now=0)
        first = lease(self.conn, "crashed", now=0, lease_seconds=5)[0]
        self.assertEqual(lease(self.conn, "w2", now=1), [])
        again = lease(self.conn, "w2", now=6)[0]
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(complete(self.conn, first["id"], "crashed", now=7))
        self.assertTrue(complete(self.conn, first["id"], "w2", now=7))

    def test_backoff_then_dead(self):
        enqueue(self.conn, "crawl", "a", host="a.gov", now=0)
        now = 0
        for attempt in range(1, 4):
            item = lease(self.conn, "w", now=now)[0]
            state = fail(self.conn, item["id"], "w", "HTTP 503", now=now, max_attempts=3, base_delay=10)
            if attempt < 3:
                self.assertEqual(state, "QUEUED")
                self.assertEqual(lease(self.conn, "w", now=now + 1), [])  # backing off
                now += 10 * 2 ** (attempt - 1)
        self.assertEqual(state, "DEAD")

    def test_bounded_benchmark_fairness_host_ceiling_and_exactly_once(self):
        rng = random.Random(7)
        hosts = [f"h{i}.gov" for i in range(5)]
        for i in range(600):
            enqueue(self.conn, "doc", f"k{i}", host=rng.choice(hosts),
                    agency="big" if i < 500 else f"small{i % 5}", now=0)
        done, now, max_per_host = [], 1.0, 0
        while True:
            batch = []
            for worker in ("w1", "w2", "w3"):
                batch += [(worker, item) for item in lease(self.conn, worker, now=now, limit=4, per_host_limit=2)]
            if not batch:
                break
            per_host = {}
            for _, item in batch:
                per_host[item["host"]] = per_host.get(item["host"], 0) + 1
            max_per_host = max(max_per_host, max(per_host.values()))
            if now == 1.0:
                # Fairness: agencies with no completed work are not starved by the big one.
                self.assertTrue(any(item["agency"] != "big" for _, item in batch))
            for worker, item in batch:
                if rng.random() < 0.05:
                    fail(self.conn, item["id"], worker, "transient", now=now, base_delay=0.5)
                else:
                    self.assertTrue(complete(self.conn, item["id"], worker, now=now))
                    done.append(item["key"])
            now += 1
        self.assertEqual(len(done), len(set(done)))
        self.assertEqual(stats(self.conn)["DONE"] + stats(self.conn)["DEAD"], 600)
        self.assertLessEqual(max_per_host, 2)


class MigrationGateTests(unittest.TestCase):
    def test_estimate_requires_caller_prices_and_never_authorizes(self):
        estimate = storage_cost_estimate(stored_bytes=1_200_000_000_000, price_per_tb_month=6.0,
                                         free_bytes=10_000_000_000)
        self.assertEqual((estimate["monthly_usd"], estimate["spend_authorized"]), (7.14, False))
        with self.assertRaises(TypeError):
            storage_cost_estimate(stored_bytes=1)  # no implicit price

    def test_gate_closed_without_explicit_sufficient_authorization(self):
        estimate = storage_cost_estimate(stored_bytes=2 * 10**12, price_per_tb_month=6.0)
        self.assertEqual(migration_gate(estimate, None)["reasons"], ["no_owner_authorization"])
        low = migration_gate(estimate, {"approved_by": "owner", "approved_at": "2026-11-15T00:00:00Z",
                                        "monthly_budget_usd": 5})
        self.assertEqual(low["reasons"], ["budget_below_estimate"])
        ok = migration_gate(estimate, {"approved_by": "owner", "approved_at": "2026-11-15T00:00:00Z",
                                       "monthly_budget_usd": 20})
        self.assertTrue(ok["open"])

    def test_shadow_compare_detects_missing_results(self):
        index = {"budget": ["a", "b", "c"], "contract": ["d"]}
        same = shadow_compare(index, index.get, index.get)
        self.assertTrue(same["exact_order_parity"])
        self.assertFalse(same["cutover_ready"])
        lossy = shadow_compare(index, index.get, lambda q: index[q][:-1])
        self.assertFalse(lossy["result_parity"])
        self.assertEqual(lossy["details"][0]["missing_in_candidate"], ["c"])


if __name__ == "__main__":
    unittest.main()

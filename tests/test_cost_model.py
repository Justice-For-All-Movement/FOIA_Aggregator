"""Cost model arithmetic stays consistent (no network)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("cost_model", Path(__file__).parent.parent / "scripts" / "cost_model.py")
cost_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost_model)


class CostModelTests(unittest.TestCase):
    def test_scenarios_are_ordered_and_consistent(self):
        low, mid, high = (cost_model.scenario(i) for i in range(3))
        for key in ("historic_originals_tb", "one_time_ocr_cloud_usd", "storage_usd_per_month_at_full_backfill"):
            self.assertLess(low[key], mid[key])
            self.assertLess(mid[key], high[key])
        self.assertAlmostEqual(mid["historic_originals_tb"], 4_000_000 * 120 * 100_000 / 1e12)
        self.assertLess(mid["one_time_ocr_self_hosted_usd"], mid["one_time_ocr_cloud_usd"])

    def test_pages_override(self):
        self.assertEqual(cost_model.scenario(1, pages_override=10)["historic_pages"], 40_000_000)


if __name__ == "__main__":
    unittest.main()

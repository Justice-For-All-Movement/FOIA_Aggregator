"""Offline gate tests for scripts/promote_audited_recovery.py (no B2 contact)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import promote_audited_recovery as promote

SAFE = {
    "candidate_appears_safe_for_archived_objects": True,
    "b2_backup_document_urls_missing_from_candidate": 0,
    "candidate_archive_rows": 10,
    "latest_b2_archive_rows": 10,
}


class PromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.cwd)

    def _candidate(self):
        Path("recovery").mkdir()
        Path("recovery/foia_archive.db").write_bytes(b"fixture")

    def test_missing_candidate_stops_before_audit(self):
        with patch.object(promote, "audit") as audit, \
             patch.object(promote, "backup_database_to_b2") as backup:
            with self.assertRaises(SystemExit):
                promote.main()
        audit.assert_not_called()
        backup.assert_not_called()

    def test_non_monotonic_candidates_never_publish(self):
        self._candidate()
        unsafe = (
            {"candidate_appears_safe_for_archived_objects": False},
            {"b2_backup_document_urls_missing_from_candidate": 1},
            {"candidate_archive_rows": 9},
        )
        for change in unsafe:
            with self.subTest(change=change), \
                 patch.object(promote, "audit", return_value={**SAFE, **change}), \
                 patch.object(promote, "backup_database_to_b2") as backup:
                with self.assertRaises(SystemExit):
                    promote.main()
                backup.assert_not_called()


class WorkflowInertnessTests(unittest.TestCase):
    def test_promotion_workflow_is_dispatch_only_and_gated(self):
        text = (Path(__file__).resolve().parents[1]
                / ".github/workflows/promote-audited-recovery.yml").read_text()
        on_block = text.split("on:", 1)[1].split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch", on_block)
        self.assertNotIn("push", on_block)
        self.assertNotIn("schedule", on_block)
        self.assertIn("vars.FOIA_RECOVERY_PROMOTION_ENABLED == 'true'", text)
        self.assertIn("inputs.confirm == 'PROMOTE'", text)
        self.assertIn("group: foia-b2-single-writer", text)


if __name__ == "__main__":
    unittest.main()

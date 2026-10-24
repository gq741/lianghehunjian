from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio.service import Studio


class StartupTests(unittest.TestCase):
    def test_second_studio_does_not_interrupt_first_instances_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Studio(Path(temp))
            try:
                first.store.create_job("active-fixture", "render", {})
                first.store.update_job("active-fixture", status="running")
                with self.assertRaisesRegex(ValueError, "已有程序"):
                    Studio(Path(temp))
                self.assertEqual(first.store.job("active-fixture")["status"], "running")
            finally:
                first.close()
            second = Studio(Path(temp))
            try:
                self.assertEqual(second.store.job("active-fixture")["status"], "interrupted")
            finally:
                second.close()

    def test_failed_initialization_releases_data_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("mixstudio.service.Settings", side_effect=ValueError("invalid settings")):
                with self.assertRaises(ValueError):
                    Studio(Path(temp))
            studio = Studio(Path(temp))
            studio.close()

    def test_invalid_duplicate_draft_is_rejected_before_paid_task(self):
        with tempfile.TemporaryDirectory() as temp:
            studio = Studio(Path(temp))
            try:
                studio.store.put_draft({"id": "broken", "clips": [{"clip_id": "a"}],
                    "segments": [{"clip_id": "a", "text": "甲"}, {"clip_id": "a", "text": "乙"}]})
                with self.assertRaisesRegex(ValueError, "不重复"):
                    studio.render("broken", True)
                self.assertEqual(studio.store.jobs(), [])
            finally:
                studio.close()


if __name__ == "__main__":
    unittest.main()

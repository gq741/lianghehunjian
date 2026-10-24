import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from mixstudio.settings import Settings, DEFAULTS
from mixstudio.store import Store
from mixstudio.jobs import JobManager, Cancelled


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_validation(self):
        s = Settings(self.root)
        self.assertEqual(s.public()["target_seconds"], 25)
        self.assertEqual(s.public()["voice_id"], "be40aff9ee7b4d0cba3bb35e924d85b2")
        for patch in [{"output_count": 0}, {"render_concurrency": 9}, {"bogus": 1},
                      {"encoder": "shell"}, {"transition_seconds": float("nan")}]:
            with self.assertRaises(ValueError):
                s.update(patch)

    def test_public_settings_never_expose_keys(self):
        s = Settings(self.root)
        if __import__("os").name == "nt":
            s.update({"deepseek_api_key": "unit-test-only-dummy"})
            public = s.public()
            self.assertTrue(public["deepseek_key_set"])
            self.assertNotIn("unit-test-only-dummy", json.dumps(public))
            self.assertNotIn("unit-test-only-dummy", (self.root / "secrets.dpapi").read_text())
            self.assertEqual(s.secret("deepseek"), "unit-test-only-dummy")
            s.update({"deepseek_api_key": ""})
            self.assertEqual(s.secret("deepseek"), "unit-test-only-dummy")

    def test_store_persistence_and_interrupt(self):
        db = Store(self.root / "state.sqlite")
        db.put_asset({"id": "a", "name": "one", "imported_at": 1})
        db.put_asset({"id": "b", "name": "two", "imported_at": 2})
        self.assertEqual(db.assets()[0]["id"], "b")
        db.create_job("j", "scan", {"folder": "x"})
        db.update_job("j", status="running", progress=.5)
        Store(self.root / "state.sqlite").interrupt_unfinished()
        self.assertEqual(db.job("j")["status"], "interrupted")

    def test_jobs_background_and_cancel(self):
        db = Store(self.root / "state.sqlite")
        jobs = JobManager(db, workers=1)
        started = threading.Event()
        def work(cancel, progress):
            started.set()
            while not cancel.wait(.02):
                progress(.2, "working")
            raise Cancelled("已取消")
        j = jobs.submit("scan", {}, work)
        self.assertTrue(started.wait(2))
        jobs.cancel(j["id"])
        deadline = time.time() + 3
        while db.job(j["id"])["status"] not in ["cancelled", "failed"] and time.time() < deadline:
            time.sleep(.02)
        self.assertEqual(db.job(j["id"])["status"], "cancelled")
        jobs.shutdown()


if __name__ == "__main__":
    unittest.main()

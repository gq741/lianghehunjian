"""HTTP -> bounded jobs -> imported assets, with offline media/provider doubles.

Real network/MP4/UI evidence is produced separately by the live acceptance tool.
"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mixstudio import media, providers
from mixstudio.http_app import create_app
from mixstudio.service import Studio


class DownloadPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "下载素材"
        self.studio = Studio(self.root / "运行数据")
        self.client = create_app(self.studio, token="offline-test-token").test_client()
        self.headers = {"X-Local-Token": "offline-test-token"}
        self.client.get("/")
        self.media_patch = patch.object(media, "probe", return_value={
            "duration": 20.0, "width": 720, "height": 1280, "has_audio": True})
        self.thumb_patch = patch.object(media, "thumbnail",
            side_effect=lambda source, target: Path(target).write_bytes(b"mock-jpeg"))
        self.media_patch.start()
        self.thumb_patch.start()

    def tearDown(self):
        self.studio.close()
        self.media_patch.stop()
        self.thumb_patch.stop()
        self.temp.cleanup()

    def submit(self, links):
        return self.client.post("/api/downloads", headers=self.headers,
            json={"links": links, "folder": str(self.folder)})

    def wait(self, jobs):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            rows = [self.studio.store.job(j["id"]) for j in jobs]
            if all(j["status"] in {"completed", "failed", "cancelled"} for j in rows):
                return rows
            time.sleep(.01)
        self.fail("download fixture timeout")

    def download(self, url, folder, **kwargs):
        path = folder / (url.rstrip("/").rsplit("/", 1)[-1] + ".mp4")
        path.write_bytes(b"offline-fixture-" + url.encode())
        return {"path": str(path), "title": "测试视频"}

    def test_batch_auto_import_and_http_media_without_scan(self):
        active = peak = 0
        lock = threading.Lock()
        def download(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.12)
                return self.download(*args, **kwargs)
            finally:
                with lock:
                    active -= 1
        links = [f"https://v.douyin.com/batch{i}/" for i in range(5)]
        with patch.object(providers, "download_douyin", side_effect=download), \
             patch.object(self.studio, "scan", side_effect=AssertionError("must not scan")):
            response = self.submit(links)
            self.assertEqual(response.status_code, 200)
            jobs = self.wait(response.json["data"])
            self.assertEqual([j["status"] for j in jobs], ["completed"] * 5, jobs)
        state = self.client.get("/api/state", headers=self.headers).json["data"]
        self.assertEqual(peak, 2)
        self.assertEqual(state["folder"], "")  # V2 download never changes local selection.
        self.assertTrue(all(a['libraries'] == ['douyin'] for a in state['assets']))
        self.assertEqual(len(state["assets"]), 5)
        self.assertEqual({a["id"] for a in state["assets"]},
                         {j["result"]["asset_id"] for j in jobs})
        times = [a["imported_at"] for a in state["assets"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertTrue(all("payload" not in j for j in state["jobs"]))
        for asset in state["assets"]:
            for key in ("thumbnail_url", "preview_url"):
                response = self.client.get(asset[key])
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_full_share_text_deduplicates_tracking_parameters(self):
        text = ("9.79 复制打开抖音，看看【张百万的家🏠的作品】"
                "小米电视 ... https://v.douyin.com/-M7p3VkY91I/?share=1 :1pm")
        with patch.object(providers, "download_douyin", side_effect=self.download) as mock:
            response = self.submit([text, "https://v.douyin.com/-M7p3VkY91I/"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json["data"]), 1)
            self.assertEqual(self.wait(response.json["data"])[0]["status"], "completed")
            self.assertEqual(mock.call_args.args[0], "https://v.douyin.com/-M7p3VkY91I/")

    def test_entire_batch_validated_before_enqueue(self):
        for bad in [None, 123, "https://v.douyin.com/",
                    "https://www.douyin.com/video/12345/extra",
                    "https://www.douyin.com/note/123456",
                    "https://v.douyin.com/aabb/ https://v.douyin.com/ccdd/"]:
            response = self.submit(["https://v.douyin.com/valid/", bad])
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.studio.store.jobs(), [])
        self.assertFalse(self.folder.exists())

    def test_failed_download_is_not_imported_and_retry_imports_once(self):
        with patch.object(providers, "download_douyin",
                          side_effect=providers.ProviderError("链接暂不可用")):
            response = self.submit(["https://v.douyin.com/failed/"])
            failed = self.wait(response.json["data"])[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.studio.state()["assets"], [])
        with patch.object(providers, "download_douyin", side_effect=self.download):
            retried = self.client.post(f"/api/jobs/{failed['id']}/retry",
                                       headers=self.headers, json={})
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(self.wait([retried.json["data"]])[0]["status"], "completed")
        self.assertEqual(len(self.studio.state()["assets"]), 1)

    def test_cancel_while_waiting_for_slot_does_not_download_or_import(self):
        self.studio.download_slot.acquire()
        self.studio.download_slot.acquire()
        try:
            with patch.object(providers, "download_douyin") as mock:
                response = self.submit(["https://v.douyin.com/cancel/"])
                job = response.json["data"][0]
                self.client.post(f"/api/jobs/{job['id']}/cancel", headers=self.headers, json={})
                result = self.wait([job])[0]
                self.assertEqual(result["status"], "cancelled")
                mock.assert_not_called()
                self.assertEqual(self.studio.state()["assets"], [])
        finally:
            self.studio.download_slot.release()
            self.studio.download_slot.release()

    def test_download_dependency_is_ffmpeg_not_ytdlp(self):
        with patch("mixstudio.service.shutil.which", return_value="tool"):
            self.assertTrue(self.studio.environment()["download_available"])
            self.assertEqual(self.studio.environment()["download_route"], "mobile_feed")
        with patch("mixstudio.service.shutil.which", return_value=None):
            self.assertFalse(self.studio.environment()["download_available"])

    def test_cancel_during_thumbnail_does_not_import(self):
        started, release = threading.Event(), threading.Event()
        def thumbnail(source, target):
            started.set()
            self.assertTrue(release.wait(3))
            Path(target).write_bytes(b"mock-jpeg")
        with patch.object(providers, "download_douyin", side_effect=self.download), \
             patch.object(media, "thumbnail", side_effect=thumbnail):
            response = self.submit(["https://v.douyin.com/cancelimport/"])
            job = response.json["data"][0]
            try:
                self.assertTrue(started.wait(3))
                self.studio.jobs.cancel(job["id"])
            finally:
                release.set()
            self.assertEqual(self.wait([job])[0]["status"], "cancelled")
        self.assertEqual(self.studio.state()["assets"], [])

    def test_late_cancel_after_asset_commit_still_reports_completed(self):
        original = self.studio.store.put_asset
        def put_asset(asset):
            result = original(asset)
            # Deterministic race: cancellation immediately after durable commit.
            for job in self.studio.store.jobs():
                if job["kind"] == "download":
                    self.studio.jobs.cancel(job["id"])
            return result
        with patch.object(providers, "download_douyin", side_effect=self.download), \
             patch.object(self.studio.store, "put_asset", side_effect=put_asset):
            response = self.submit(["https://v.douyin.com/committed/"])
            result = self.wait(response.json["data"])[0]
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["result"]["asset_id"], self.studio.state()["assets"][0]["id"])


if __name__ == "__main__":
    unittest.main()

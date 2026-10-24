"""Review regressions. All provider/media work is mocked; no paid API calls.

These assert the desired invariant, not the observed buggy behavior. Before the
service fixes they intentionally fail: shared paid cache must be single-flight,
uncertain requests must not trigger queued duplicate POSTs, and ASR ranges must
remain valid after end-of-file clamping.
"""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio.service import Studio, fingerprint
from mixstudio.providers import ProviderError


class ServiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.studio = Studio(self.root / "runtime")
        self.studio.settings.update({"output_dir": str(self.root / "outputs"),
                                     "render_concurrency": 2})
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"unit-test-video-placeholder")
        self.asset_id = "source-fixture"
        self.studio.store.put_asset({
            "id": self.asset_id, "name": self.source.name, "path": str(self.source),
            "sha256": fingerprint(self.source), "duration": 10.0,
            "width": 320, "height": 480, "imported_at": 1,
            "clips": [], "transcript_status": "pending",
        })
        self.secret_patch = patch.object(self.studio.settings, "secret",
                                         return_value="unit-test-placeholder")
        self.secret_patch.start()

    def tearDown(self):
        self.studio.close()
        self.secret_patch.stop()
        self.temporary.cleanup()

    def wait_job(self, identifier):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = self.studio.store.job(identifier)
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(.01)
        self.fail("review fixture job timed out")

    def drafts(self):
        asset = self.studio.set_transcript(self.asset_id, [
            {"start": 0, "end": 10, "text": "测试商品同一句说明。"}])
        for number in (1, 2):
            self.studio.store.put_draft({
                "id": f"draft-{number}", "title": f"version-{number}", "product_name": "测试商品",
                "asset_ids": [self.asset_id], "target_seconds": 25, "revision": 1,
                "status": "draft", "created_at": number, "clips": asset["clips"],
                "segments": [{"clip_id": asset["clips"][0]["clip_id"],
                              "text": "测试商品同一句说明。"}],
            })

    @staticmethod
    def media_fixture():
        def render(clips, voice, output, settings, **kwargs):
            Path(output).write_bytes(b"mock-render-output")
            return {"path": str(output), "duration": 1.0, "warnings": []}
        return SimpleNamespace(probe=lambda path: {"duration": 1.0, "has_audio": True},
                               render_video=render)

    @staticmethod
    def concat_fixture(paths, output, event):
        Path(output).write_bytes(b"mock-concatenated-audio")

    def test_parallel_shared_sentence_posts_once_and_reuses_validated_cache(self):
        self.drafts()
        calls, lock = [], threading.Lock()
        first_started = threading.Event()
        def synthesize(text, output, **kwargs):
            with lock:
                calls.append(text)
            first_started.set()
            # Keep the first paid operation in flight while second render begins.
            time.sleep(.35)
            Path(output).write_bytes(b"mock-voice")
            return {"path": str(output), "duration": 1.0}
        providers = SimpleNamespace(synthesize_voice=synthesize, ProviderError=ProviderError)
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)), \
             patch.object(self.studio, "_concat_voice", side_effect=self.concat_fixture):
            first = self.studio.render("draft-1", True)
            self.assertTrue(first_started.wait(2))
            second = self.studio.render("draft-2", True)
            jobs = [self.wait_job(j["id"]) for j in (first, second)]
        self.assertEqual([j["status"] for j in jobs], ["completed", "completed"], jobs)
        self.assertEqual(len(calls), 1, "same voice cache key caused two billable requests")

    def test_queued_shared_sentence_does_not_resubmit_ambiguous_paid_failure(self):
        self.drafts()
        calls, lock = [], threading.Lock()
        first_started = threading.Event()
        def synthesize(text, output, **kwargs):
            with lock:
                calls.append(text)
            first_started.set()
            time.sleep(.35)
            raise ProviderError("服务端结果待确认，未自动重试。",
                                code="network", result_unknown=True)
        providers = SimpleNamespace(synthesize_voice=synthesize, ProviderError=ProviderError)
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)), \
             patch.object(self.studio, "_concat_voice", side_effect=self.concat_fixture):
            first = self.studio.render("draft-1", True)
            self.assertTrue(first_started.wait(2))
            second = self.studio.render("draft-2", True)
            jobs = [self.wait_job(j["id"]) for j in (first, second)]
        self.assertEqual([j["status"] for j in jobs], ["failed", "failed"])
        self.assertEqual(len(calls), 1, "queued waiter repeated an uncertain paid request")
        self.assertTrue(all(j.get("result_unknown") for j in jobs), jobs)

    def test_cancelled_paid_request_keeps_unknown_warning_and_marker(self):
        self.drafts()
        started = threading.Event()
        def synthesize(text, output, *, cancel, **kwargs):
            started.set()
            self.assertTrue(cancel.wait(2))
            raise ProviderError("本地取消，服务端结果待确认。",
                                code="cancelled", result_unknown=True)
        providers = SimpleNamespace(synthesize_voice=synthesize, ProviderError=ProviderError)
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)):
            job = self.studio.render("draft-1", True)
            self.assertTrue(started.wait(2))
            self.studio.jobs.cancel(job["id"])
            finished = self.wait_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertTrue(finished["result_unknown"])
        self.assertIn("服务端结果待确认", finished["message"])
        self.assertTrue(list((self.studio.root / "voice-cache").glob("*.pending.json")))

    def test_unknown_request_requires_explicit_retry_not_normal_render(self):
        self.drafts()
        calls = []
        def synthesize(text, output, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                raise ProviderError("服务端结果待确认。", code="network", result_unknown=True)
            Path(output).write_bytes(b"mock-voice")
            return {"path": str(output), "duration": 1}
        providers = SimpleNamespace(synthesize_voice=synthesize, ProviderError=ProviderError)
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)), \
             patch.object(self.studio, "_concat_voice", side_effect=self.concat_fixture):
            first = self.studio.render("draft-1", True)
            self.assertEqual(self.wait_job(first["id"])["status"], "failed")
            normal = self.studio.render("draft-2", True)
            blocked = self.wait_job(normal["id"])
            self.assertEqual(blocked["status"], "failed")
            self.assertTrue(blocked["result_unknown"])
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ValueError):
                self.studio.retry(first["id"], allow_paid=False)
            retry = self.studio.retry(first["id"], allow_paid=True)
            self.assertEqual(self.wait_job(retry["id"])["status"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertFalse(list((self.studio.root / "voice-cache").glob("*.pending.json")))

    def test_transcript_post_eof_ranges_never_become_reversed_or_zero_length(self):
        for start, end in ((10.05, 10.08), (10.0, 10.05)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.studio.set_transcript(self.asset_id,
                                           [{"start": start, "end": end, "text": "越界片段"}])

    def test_small_end_rounding_tolerance_still_clamps_to_valid_range(self):
        asset = self.studio.set_transcript(self.asset_id,
                                          [{"start": 9.5, "end": 10.05, "text": "有效尾句"}])
        self.assertEqual(asset["clips"][0]["end"], 10.0)
        self.assertLess(asset["clips"][0]["start"], asset["clips"][0]["end"])

    def test_serialized_asr_rechecks_transcript_before_repeating_inference(self):
        calls = []
        first_started = threading.Event()
        def transcribe(*args, **kwargs):
            calls.append(1)
            first_started.set()
            time.sleep(.35)
            return [{"start": 0, "end": 5, "text": "商品原文"}]
        def rewrite(clips, **kwargs):
            return [{"title": "测试草稿", "segments": [
                {"clip_id": clips[0]["clip_id"], "text": "商品原文"}]}]
        providers = SimpleNamespace(transcribe_video=transcribe, rewrite_clips=rewrite)
        payload = {"asset_ids": [self.asset_id], "product_name": "测试商品",
                   "target_seconds": 25, "count": 1, "allow_paid": True}
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)):
            first = self.studio.prepare(payload)
            self.assertTrue(first_started.wait(2))
            second = self.studio.prepare(payload)
            jobs = [self.wait_job(j["id"]) for j in (first, second)]
        self.assertEqual([j["status"] for j in jobs], ["completed", "completed"], jobs)
        self.assertEqual(len(calls), 1, "queued prepare used stale clips=[] and repeated ASR")

    def test_manual_transcript_saved_during_asr_is_not_overwritten(self):
        started, release = threading.Event(), threading.Event()
        observed = []
        def transcribe(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return [{"start": 0, "end": 5, "text": "自动结果"}]
        def rewrite(clips, **kwargs):
            observed.extend(c["text"] for c in clips)
            return [{"title": "测试草稿", "segments": [
                {"clip_id": clips[0]["clip_id"], "text": clips[0]["text"]}]}]
        providers = SimpleNamespace(transcribe_video=transcribe, rewrite_clips=rewrite)
        payload = {"asset_ids": [self.asset_id], "product_name": "测试商品",
                   "target_seconds": 25, "count": 1, "allow_paid": True}
        with patch.object(self.studio, "_modules", return_value=(self.media_fixture(), providers)):
            job = self.studio.prepare(payload)
            self.assertTrue(started.wait(2))
            self.studio.set_transcript(self.asset_id, [{"start": 0, "end": 5, "text": "用户校对结果"}])
            release.set()
            self.assertEqual(self.wait_job(job["id"])["status"], "completed")
        self.assertEqual(observed, ["用户校对结果"])
        self.assertEqual(self.studio.store.asset(self.asset_id)["clips"][0]["text"], "用户校对结果")


if __name__ == "__main__":
    unittest.main()

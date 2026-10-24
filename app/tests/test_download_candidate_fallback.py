"""Exact-video CDN fallback regressions; every network response is an offline fixture."""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mixstudio import providers as p
from mixstudio.service import Studio


class Response:
    def __init__(self, body=b"", *, status=200, headers=None, on_close=None):
        self.body = io.BytesIO(body)
        self.status, self.headers = status, headers or {}
        self.closed = False
        self.on_close = on_close

    def read1(self, count):
        return self.body.read(count)

    def close(self):
        self.closed = True
        if self.on_close:
            self.on_close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


VIDEO_ID = "123456789"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
URLS = tuple(f"https://v{i}.douyinvod.com/media.mp4?signature=fixture-not-for-log"
             for i in range(1, 5))
MP4 = b"\0\0\0\x18ftypisom\0\0\0\0isomiso2" + b"fixture-media"


def metadata(urls=URLS, duration=5000):
    return {"status_code": 0, "aweme_list": [{
        "aweme_id": VIDEO_ID, "desc": "备用CDN测试",
        "video": {"duration": duration, "play_addr": {"url_list": list(urls)}},
    }]}


def feed_response(urls=URLS, duration=5000):
    return Response(json.dumps(metadata(urls, duration)).encode())


class DownloadCandidateFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.dns = self.enterContext(patch.object(p, "_public_download_addresses", return_value=[]))
        self.open = self.enterContext(patch.object(p, "_open_public_download"))
        self.enterContext(patch.object(p.shutil, "which", side_effect=lambda name: name))
        self.process = self.enterContext(patch.object(p, "_run_process", side_effect=self.fake_process))
        # Fail closed if an unmocked network path is accidentally introduced.
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")))

    @staticmethod
    def fake_process(args, **_):
        if "-show_streams" in args:
            return 0, json.dumps({
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.0"},
                "streams": [{"codec_type": "video", "width": 720, "height": 1280},
                            {"codec_type": "audio"}],
            })
        return 0, ""

    def download(self, **kwargs):
        return p.download_douyin(VIDEO_URL, self.folder, **kwargs)

    def assert_empty(self):
        self.assertEqual(list(self.folder.iterdir()), [])

    def requested_media(self):
        return [call.args[0] for call in self.open.call_args_list
                if "aweme.snssdk.com" not in call.args[0]]

    def test_feed_preserves_order_deduplicates_and_bounds_legal_candidates(self):
        urls = ["http://v1.douyinvod.com/plain", URLS[0], URLS[0],
                "https://evil.test/not-platform", *URLS[1:]]
        self.open.return_value = feed_response(urls)
        title, candidates, duration = p._feed_video(
            VIDEO_ID, cancel=None, deadline=time.monotonic() + 10)
        self.assertEqual(candidates, URLS[:3])
        self.assertIsInstance(candidates, tuple)
        self.assertEqual((title, duration), ("备用CDN测试", 5.0))
        self.assertEqual(self.open.call_count, 1)

    def test_cdn_forbidden_missing_gone_or_server_failure_uses_next_candidate(self):
        for status in (403, 404, 410, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.open.reset_mock()
                denied = Response(status=status)
                self.open.side_effect = [feed_response(), denied, Response(MP4)]
                result = self.download()
                target = Path(result["path"])
                self.assertEqual(target.read_bytes(), MP4)
                self.assertEqual(self.requested_media(), list(URLS[:2]))
                self.assertTrue(denied.closed)
                self.assertEqual(len(list(self.folder.iterdir())), 1)
                target.unlink()

    def test_all_http_candidates_fail_after_at_most_three_without_query_leak(self):
        self.open.side_effect = [feed_response(), *[Response(status=403) for _ in URLS]]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(self.requested_media(), list(URLS[:3]))
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("signature", str(caught.exception))
        self.assertNotIn("fixture-not-for-log", str(caught.exception))
        self.assert_empty()

    def test_unauthorized_and_rate_limit_stop_without_backup(self):
        for status in (401, 429):
            with self.subTest(status=status):
                self.open.reset_mock()
                self.open.side_effect = [feed_response(), Response(status=status), Response(MP4)]
                with self.assertRaises(p.ProviderError) as caught:
                    self.download()
                self.assertEqual(self.requested_media(), [URLS[0]])
                self.assertIn(str(status), str(caught.exception))
                self.assert_empty()

    def test_http_error_identifies_feed_stage_without_media_request(self):
        self.open.side_effect = [Response(status=403)]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertIn("403", str(caught.exception))
        self.assertEqual(self.open.call_count, 1)
        self.assert_empty()

    def test_first_success_does_not_touch_backups(self):
        self.open.side_effect = [feed_response(), Response(MP4)]
        result = self.download()
        self.assertEqual(Path(result["path"]).read_bytes(), MP4)
        self.assertEqual(self.requested_media(), [URLS[0]])

    def test_two_transport_attempts_then_backup_and_partial_cleanup(self):
        responses = [feed_response(),
                     Response(MP4[:8], headers={"Content-Length": str(len(MP4))}),
                     Response(MP4[:8], headers={"Content-Length": str(len(MP4))}),
                     Response(MP4)]
        def next_response(*_, **__):
            self.assert_empty()  # The previous candidate/retry must remove its .part.
            return responses.pop(0)
        self.open.side_effect = next_response
        result = self.download()
        self.assertEqual(self.requested_media(), [URLS[0], URLS[0], URLS[1]])
        self.assertEqual(Path(result["path"]).read_bytes(), MP4)
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        deadlines = {call.kwargs["deadline"] for call in self.open.call_args_list}
        self.assertEqual(len(deadlines), 1)

    def test_transport_failure_is_bounded_to_two_attempts_per_three_candidates(self):
        self.open.side_effect = [feed_response(), *[Response() for _ in range(8)]]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "download_transport")
        self.assertEqual(self.requested_media(), [url for url in URLS[:3] for _ in range(2)])
        self.assertEqual(self.open.call_count, 7)
        self.assert_empty()

    def test_tls_error_never_tries_backup(self):
        self.open.side_effect = [feed_response(),
                                p.ProviderError("证书校验失败", code="download_tls"), Response(MP4)]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "download_tls")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()

    def test_private_dns_error_never_tries_backup(self):
        self.open.side_effect = [feed_response(), Response(MP4)]
        self.dns.side_effect = [[], p.ProviderError("非公网地址", code="invalid_url"), []]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(self.dns.call_count, 2)
        self.assert_empty()

    def test_invalid_redirect_never_tries_backup(self):
        self.open.side_effect = [feed_response(),
                                Response(status=302, headers={"Location": "https://evil.test/private"}),
                                Response(MP4)]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()

    def test_invalid_body_is_not_hidden_by_backup(self):
        self.open.side_effect = [feed_response(), Response(b"<html>not video</html>"), Response(MP4)]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "invalid_download")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()

    def test_decode_failure_is_not_hidden_by_backup(self):
        self.open.side_effect = [feed_response(), Response(MP4), Response(MP4)]
        self.process.side_effect = [self.fake_process(["-show_streams"]), (1, "decode failed")]
        with self.assertRaises(p.ProviderError) as caught:
            self.download()
        self.assertEqual(caught.exception.code, "invalid_download")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()

    def test_feed_wrong_identity_stops_before_all_candidates(self):
        meta = metadata()
        meta["aweme_list"][0]["aweme_id"] = "999999999"
        self.open.side_effect = [Response(json.dumps(meta).encode()), Response(MP4)]
        with self.assertRaises(p.ProviderError):
            self.download()
        self.assertEqual(self.open.call_count, 1)
        self.assert_empty()

    def test_cancel_between_candidates_does_not_contact_backup(self):
        cancelled = threading.Event()
        self.open.side_effect = [feed_response(),
                                Response(status=403, on_close=cancelled.set), Response(MP4)]
        with self.assertRaises(p.ProviderError) as caught:
            self.download(cancel=cancelled)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()

    def test_candidates_share_total_deadline_not_a_fresh_timeout(self):
        clock = [1000.0]
        def expire():
            clock[0] += p.DOWNLOAD_TOTAL_SECONDS + 1
        self.open.side_effect = [feed_response(), Response(status=403, on_close=expire), Response(MP4)]
        with patch.object(p.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(p.ProviderError) as caught:
                self.download()
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(self.requested_media(), [URLS[0]])
        self.assert_empty()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class DownloadCandidateImportTests(unittest.TestCase):
    def test_fallback_real_mp4_validates_and_imports_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.mp4"
            command = [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error",
                       "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=0.5",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.5",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-movflags", "+faststart", "-shortest", str(fixture)]
            completed = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            body = fixture.read_bytes()
            studio = Studio(root / "runtime")
            try:
                with patch.object(p, "_public_download_addresses", return_value=[]), \
                     patch.object(p, "_open_public_download", side_effect=[
                         feed_response(duration=500), Response(status=403), Response(body)]) as opened, \
                     patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")):
                    jobs = studio.download([VIDEO_URL], str(root / "downloads"))
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        job = studio.store.job(jobs[0]["id"])
                        if job["status"] in {"completed", "failed", "cancelled"}:
                            break
                        time.sleep(.02)
                    self.assertEqual(job["status"], "completed", job.get("error"))
                    self.assertEqual(opened.call_count, 3)
                assets = studio.state()["assets"]
                self.assertEqual(len(assets), 1)
                self.assertEqual(assets[0]["libraries"], ["douyin"])
                self.assertEqual(job["result"]["asset_id"], assets[0]["id"])
                files = list((root / "downloads").iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_bytes(), body)
                self.assertEqual(fixture.read_bytes(), body)
            finally:
                studio.close()


if __name__ == "__main__":
    unittest.main()

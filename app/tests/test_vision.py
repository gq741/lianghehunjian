"""Offline V2 vision tests: generated media is real; GLM/legacy Qwen responses are mocked."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from mixstudio import providers, vision


def observation(cid, **updates):
    result = {"clip_id": cid, "description": "手持展示电视的边框与接口", "tags": ["外观", "接口"],
              "ocr": "小米电视", "same_product": "yes", "flags": [], "confidence": .9,
              "reason": "画面中可见商品与目标类别及品牌一致"}
    result.update(updates)
    return result


def envelope(observations, finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps({"observations": observations}, ensure_ascii=False)}}]}, ensure_ascii=False).encode(), "application/json"


def respond(url, payload, api_key, **kwargs):
    clips = json.loads(payload["messages"][1]["content"][0]["text"])["clips"]
    return envelope([observation(c["clip_id"]) for c in clips])


class VisionUnitTests(unittest.TestCase):
    def test_official_endpoint_and_visual_model_only(self):
        for url, model in ((vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL),
                           (vision.DEFAULT_ENDPOINT + "/", vision.DEFAULT_MODEL),
                           ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-vl-plus")):
            self.assertEqual(vision.validate_config(url, model)[0], url.rstrip("/"))
        for url in ("http://open.bigmodel.cn/api/paas/v4/chat/completions",
                    vision.DEFAULT_ENDPOINT + "?key=abc", vision.DEFAULT_ENDPOINT + "#fragment",
                    vision.DEFAULT_ENDPOINT.replace("open.bigmodel.cn", "evil.example"),
                    vision.DEFAULT_ENDPOINT.replace("https://", "https://key@"),
                    vision.DEFAULT_ENDPOINT.replace(".cn/", ".cn:444/"), None):
            with self.subTest(url=url), self.assertRaises(providers.ProviderError):
                vision.validate_config(url, vision.DEFAULT_MODEL)
        for model in ("deepseek-chat", "qwen-vl-plus", "glm-5.3-flash\n", "glm-5.3-flash/../../file"):
            with self.assertRaises(providers.ProviderError):
                vision.validate_config(vision.DEFAULT_ENDPOINT, model)

    def test_plan_bounds_and_explicit_missing_coverage(self):
        plan, warnings = vision._plan("a" * 64, 1000)
        self.assertEqual(len(plan), 90)
        self.assertEqual(len({s["clip_id"] for s in plan}), 90)
        self.assertGreaterEqual(len(warnings), 2)
        self.assertIn("未识别", warnings[1])
        for scene in plan:
            self.assertLessEqual(scene["end"] - scene["start"], 3)
            self.assertTrue(0 <= scene["start"] < scene["end"] <= 1000)
            self.assertTrue(all(scene["start"] < t < scene["end"] for t in scene["sample_times"]))
        self.assertLess(sum(s["end"] - s["start"] for s in plan), 1000)
        self.assertEqual(plan[-1]["end"], 1000)

    def test_short_video_and_tiny_tail(self):
        plan, _ = vision._plan("a" * 64, .08)
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(plan[0]["sample_times"]), 3)
        plan, warnings = vision._plan("a" * 64, 3.04)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["end"], 3)
        self.assertTrue(any("未识别" in w for w in warnings))

    def test_strict_observation_ignores_no_model_timecodes(self):
        for changes in ({"start": 900}, {"confidence": float("nan")}, {"confidence": True},
                        {"flags": ["person"]}, {"same_product": True}, {"tags": "static"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                vision._observation(observation("known", **changes), {"known"})
        with self.assertRaises(ValueError):
            vision._observation(observation("invented"), {"known"})

    def test_person_and_static_product_are_not_garbage(self):
        planned, _ = vision._plan("a" * 64, 4)
        scene = vision._scene({"id": "asset", "path": "source.mp4"}, planned[0],
                              observation(planned[0]["clip_id"], tags=["人物演示", "静态商品特写"]),
                              [{"mean": 120, "black_ratio": .1}] * 5)
        self.assertTrue(scene["usable"])

    def test_conservative_flags_and_confidence(self):
        planned, _ = vision._plan("a" * 64, 4)
        for updates in ({"ocr": "加我微信：abc123456"}, {"flags": ["qr_code"]},
                        {"flags": ["severe_blur"]}, {"flags": ["unrelated_outro"]},
                        {"confidence": .64}, {"same_product": "uncertain"}, {"same_product": "no"}):
            with self.subTest(updates=updates):
                scene = vision._scene({"id": "asset", "path": "source.mp4"}, planned[0],
                                      observation(planned[0]["clip_id"], **updates),
                                      [{"mean": 120, "black_ratio": .1}] * 5)
                self.assertFalse(scene["usable"])

    def test_local_black_frame_overrides_model_good_assessment(self):
        planned, _ = vision._plan("a" * 64, 4)
        scene = vision._scene({"id": "asset", "path": "source.mp4"}, planned[0],
                              observation(planned[0]["clip_id"]),
                              [{"mean": 120, "black_ratio": .1}, {"mean": 0, "black_ratio": 1},
                               {"mean": 120, "black_ratio": .1}])
        self.assertFalse(scene["usable"])
        self.assertIn("black", scene["flags"])


class VisionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"unit-test-source-no-real-video")
        self.asset = {"id": "asset", "path": str(self.source),
                      "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.cache = self.root / "cache"
        self.options = {"api_key": "test-not-a-real-key", "endpoint": vision.DEFAULT_ENDPOINT,
                        "model": vision.DEFAULT_MODEL, "product_title": "小米电视"}
        self.probe = patch.object(vision.media, "probe", return_value={"duration": 6, "width": 1280, "height": 720}).start()
        self.frame = patch.object(vision, "_frame", return_value=(b"\xff\xd8test\xff\xd9", {"mean": 100, "black_ratio": 0})).start()
        self.post = patch.object(vision.providers, "_post", side_effect=respond).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def analyze(self, **options):
        return vision.analyze_asset(self.asset, self.cache, **{**self.options, **options})

    def test_real_image_payload_and_local_timing(self):
        result = self.analyze()
        self.assertEqual(len(result["scenes"]), 2)
        self.assertTrue(all(s["usable"] for s in result["scenes"]))
        payload = self.post.call_args.args[1]
        content = payload["messages"][1]["content"]
        images = [x["image_url"]["url"] for x in content if x["type"] == "image_url"]
        self.assertEqual(len(images), 6)
        self.assertEqual(base64.b64decode(images[0].split(",", 1)[1]), b"\xff\xd8test\xff\xd9")
        self.assertNotIn(str(self.source), json.dumps(payload))
        self.assertEqual([(s["start"], s["end"]) for s in result["scenes"]], [(0, 3), (3, 6)])
        self.assertEqual(self.source.read_bytes(), b"unit-test-source-no-real-video")

    def test_cache_hit_no_new_api_or_frames(self):
        first = self.analyze()
        frames = self.frame.call_count
        self.assertEqual(self.analyze(), first)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.frame.call_count, frames)
        self.assertFalse(any(self.cache.glob("vision-frames-*")))

    def test_cache_separates_model_endpoint_product_and_content(self):
        self.analyze()
        self.analyze(endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", model="qwen2.5-vl-72b-instruct")
        self.analyze(endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", model="qwen-vl-plus")
        self.analyze(product_title="另一商品")
        self.source.write_bytes(b"new-source")
        self.asset["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.analyze()
        self.assertEqual(self.post.call_count, 5)
        self.assertEqual(len(list(self.cache.glob("vision-*.json"))), 5)

    def test_cache_rebinds_same_content_new_registered_path(self):
        self.analyze()
        new_source = self.root / "copy.mp4"
        new_source.write_bytes(self.source.read_bytes())
        self.asset.update(id="new-id", path=str(new_source))
        result = self.analyze()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(result["scenes"][0]["asset_id"], "new-id")
        self.assertEqual(result["scenes"][0]["source_path"], str(new_source.resolve()))

    def test_tampered_cache_no_paid_repeat(self):
        self.analyze()
        cache = next(self.cache.glob("vision-*.json"))
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["records"][0]["planned"]["start"] = 1000
        cache.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, "invalid_cache")
        self.assertEqual(self.post.call_count, 1)

    def test_incomplete_response_or_unknown_id_never_usable(self):
        for transform in (lambda values: [], lambda values: values + values,
                          lambda values: [observation("invented")],
                          lambda values: [{**v, "end": 100} for v in values]):
            def bad(url, payload, api_key, **kwargs):
                clips = json.loads(payload["messages"][1]["content"][0]["text"])["clips"]
                return envelope(transform([observation(c["clip_id"]) for c in clips]))
            self.post.side_effect = bad
            with self.assertRaises(providers.ProviderError) as error:
                self.analyze()
            self.assertEqual(error.exception.code, "invalid_vision_response")
            self.assertTrue(error.exception.result_unknown)
        self.assertEqual(self.post.call_count, 4)  # one attempt per explicit call, never automatic retry

    def test_provider_timeout_no_automatic_retry(self):
        self.post.side_effect = providers.ProviderError("timeout", code="timeout", result_unknown=True)
        with self.assertRaises(providers.ProviderError):
            self.analyze()
        self.assertEqual(self.post.call_count, 1)

    def test_truncated_provider_json_rejected(self):
        self.post.return_value = (b"{", "application/json")
        self.post.side_effect = None
        with self.assertRaises(providers.ProviderError):
            self.analyze()
        self.assertEqual(self.post.call_count, 1)

    def test_cancellation_before_request(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze(cancel=cancel)
        self.assertEqual(error.exception.code, "cancelled")
        self.post.assert_not_called()
        self.frame.assert_not_called()

    def test_source_hash_mismatch_stops_before_paid_call(self):
        self.source.write_bytes(b"changed")
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, "source_changed")
        self.post.assert_not_called()

    def test_source_change_during_sampling_stops_before_paid_call(self):
        def mutate(*args):
            self.source.write_bytes(b"changed-during-sampling")
            return b"\xff\xd8test\xff\xd9", {"mean": 100, "black_ratio": 0}
        self.frame.side_effect = mutate
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, "source_changed")
        self.post.assert_not_called()
        self.assertEqual(list(self.cache.glob("vision-*.json")), [])

    def test_source_change_during_paid_call_never_writes_ambiguous_cache(self):
        def mutate(url, payload, api_key, **kwargs):
            self.source.write_bytes(b"changed-during-provider")
            return respond(url, payload, api_key, **kwargs)
        self.post.side_effect = mutate
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, "source_changed")
        self.assertTrue(error.exception.result_unknown)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(list(self.cache.glob("vision-*.json")), [])

    def test_long_video_bounded_forty_five_calls_ninety_scenes(self):
        self.probe.return_value["duration"] = 999
        result = self.analyze()
        self.assertEqual(vision.MAX_REQUESTS, 45)
        self.assertEqual(self.post.call_count, 45)
        self.assertEqual(len(result["scenes"]), 90)
        self.assertEqual(self.frame.call_count, 270)
        self.assertIn("未识别", result["warnings"][1])
        counts = []
        for call in self.post.call_args_list:
            content = call.args[1]["messages"][1]["content"]
            counts.append(sum(item["type"] == "image_url" for item in content))
        self.assertEqual(counts, [6] * 45)

    def test_successful_batches_retained_after_later_failure(self):
        self.probe.return_value["duration"] = 999
        def fail_second(url, payload, api_key, **kwargs):
            if self.post.call_count == 2:
                raise providers.ProviderError("timeout", code="timeout", result_unknown=True)
            return respond(url, payload, api_key, **kwargs)
        self.post.side_effect = fail_second
        with self.assertRaises(providers.ProviderError):
            self.analyze()
        cache = json.loads(next(self.cache.glob("vision-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(cache["records"]), 2)
        self.post.side_effect = respond
        result = self.analyze()
        self.assertEqual(len(result["scenes"]), 90)
        self.assertEqual(self.post.call_count, 46)  # one success + one failure + 44 pending batches

    def test_qwen3_nonthinking_payload_and_visual_timeouts(self):
        for model in ("qwen3-vl-plus", "qwen3-vl-plus-2025-12-19"):
            with self.subTest(model=model):
                self.analyze(endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", model=model)
                payload = self.post.call_args.args[1]
                self.assertIs(payload.get("enable_thinking"), False)
                self.assertEqual(payload["max_tokens"], 7000)
                self.assertEqual(self.post.call_args.kwargs.get("timeout_seconds"), 120)
                self.assertEqual(self.post.call_args.kwargs.get("total_seconds"), 180)

    def test_legacy_visual_model_does_not_receive_thinking_parameter(self):
        for model in ("qwen-vl-plus", "qwen2.5-vl-72b-instruct", "qwen3-vl-235b-a22b-thinking"):
            with self.subTest(model=model):
                self.analyze(endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", model=model)
                self.assertNotIn("enable_thinking", self.post.call_args.args[1])

    def test_progress_reports_exact_pending_batches_and_image_counts(self):
        self.probe.return_value["duration"] = 15
        events = []
        self.analyze(progress=lambda fraction, message: events.append((fraction, message)))
        requests = [(fraction, message) for fraction, message in events if "正在进行画面识别" in message]
        self.assertEqual(len(requests), 3)
        self.assertIn("第 1/3 批", requests[0][1])
        self.assertIn("2 个片段 / 6 张图", requests[0][1])
        self.assertIn("第 3/3 批", requests[2][1])
        self.assertIn("1 个片段 / 3 张图", requests[2][1])
        self.assertEqual([item[0] for item in requests], [0, .4, .8])
        self.assertEqual(events[-1][0], 1)

    def test_batch_failure_has_fixed_context_preserves_code_without_raw_error(self):
        self.probe.return_value["duration"] = 20
        for code, unknown in (("timeout", True), ("dns", True), ("tls", True), ("connection", True),
                              ("http_401", False), ("unrecognized_error", True)):
            with self.subTest(code=code):
                self.post.reset_mock()
                failure = providers.ProviderError(
                    "private-upstream-response secret123", code=code, result_unknown=unknown)
                failure.request_phase = "read" if code == "timeout" else "connect"
                self.post.side_effect = failure
                with self.assertRaises(providers.ProviderError) as error:
                    self.analyze()
                self.assertEqual(error.exception.code, code)
                self.assertEqual(error.exception.result_unknown, unknown)
                self.assertEqual(error.exception.request_phase, failure.request_phase)
                self.assertIn("读取响应" if code == "timeout" else "连接或等待响应", str(error.exception))
                self.assertIn("本视频第 1/4 批", str(error.exception))
                self.assertIn("2 个片段 / 最多 6 张图", str(error.exception))
                self.assertNotIn("private-upstream", str(error.exception))
                self.assertNotIn("secret123", str(error.exception))
                if code == "connection":
                    self.assertIn("网络连接失败或中断", str(error.exception))
                self.assertEqual(self.post.call_count, 1)

    def test_partial_new_sampling_cache_reused(self):
        self.probe.return_value["duration"] = 999
        planned, _ = vision._plan(self.asset["sha256"], 999)
        identity = vision._cache_identity(self.asset['sha256'], 999,
            self.options['endpoint'], self.options['model'], self.options['product_title'])
        records = {scene["clip_id"]: {"planned": scene, "observation": observation(scene["clip_id"]),
                   "quality": [{"mean": 100, "black_ratio": 0}] * 3} for scene in planned[:13]}
        path = vision._cache_path(self.cache, identity)
        vision._save_cache(path, identity, records)
        events = []
        result = self.analyze(progress=lambda fraction, message: events.append((fraction, message)))
        self.assertEqual(vision.ALGORITHM_VERSION, 2)
        self.assertEqual(len(result["scenes"]), 90)
        self.assertEqual(self.post.call_count, 39)
        self.assertEqual(self.frame.call_count, 77 * 3)
        first_content = self.post.call_args_list[0].args[1]["messages"][1]["content"]
        first_clips = json.loads(first_content[0]["text"])["clips"]
        self.assertEqual(first_clips[0]["clip_id"], "c1")
        self.assertEqual(result["scenes"][13]["clip_id"], planned[13]["clip_id"])
        self.assertTrue(any("第 1/39 批" in message for _, message in events))
        self.assertTrue(any("复用" in message and "13" in message for _, message in events))

    def test_cancel_between_batches_keeps_success_without_next_request(self):
        self.probe.return_value["duration"] = 20
        cancel = threading.Event()
        def progress(fraction, message):
            if "正在抽取" in message and self.post.call_count == 1:
                cancel.set()
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze(cancel=cancel, progress=progress)
        self.assertEqual(error.exception.code, "cancelled")
        self.assertEqual(self.post.call_count, 1)
        cache = json.loads(next(self.cache.glob("vision-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(cache["records"]), 2)

    def test_no_secret_in_cache(self):
        self.analyze()
        cache_text = next(self.cache.glob("vision-*.json")).read_text(encoding="utf-8")
        self.assertNotIn(self.options["api_key"], cache_text)
        self.assertNotIn("image_url", cache_text)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
class RealLocalMediaTests(unittest.TestCase):
    def test_real_ffmpeg_multiframe_black_detection_original_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "black-then-testpattern.mp4"
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=3",
                "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10:d=3",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source),
            ], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = {"id": "real-local-synthetic", "path": str(source), "sha256": before}
            with patch.object(providers, "_post", side_effect=respond) as post:
                analyzed = vision.analyze_asset(asset, root / "cache", api_key="test-not-real",
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title="测试电视")
            self.assertEqual(len(analyzed["scenes"]), 2)
            self.assertFalse(analyzed["scenes"][0]["usable"])
            self.assertIn("black", analyzed["scenes"][0]["flags"])
            self.assertTrue(analyzed["scenes"][1]["usable"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            images = [x["image_url"]["url"] for x in post.call_args.args[1]["messages"][1]["content"] if x["type"] == "image_url"]
            self.assertEqual(len(images), 6)
            for image in images:
                jpeg = base64.b64decode(image.split(",", 1)[1])
                self.assertTrue(jpeg.startswith(b"\xff\xd8"))
                self.assertTrue(jpeg.endswith(b"\xff\xd9"))
                self.assertLessEqual(len(jpeg), vision.MAX_JPEG_BYTES)
            self.assertNotEqual(images[0], images[-1])


if __name__ == "__main__":
    unittest.main()

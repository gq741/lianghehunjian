"""Offline provider contract tests: never call an API or download a model."""
import io
import json
from pathlib import Path
import subprocess
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

from mixstudio import providers as p
from mixstudio import asr_worker


class Response:
    def __init__(self, body, content_type="application/json", *, headers=None):
        self.body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.headers.update(headers or {})

    def read(self, size):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.body.close()


CLIPS = [{"clip_id": "clip_1", "start": 0, "end": 3, "text": "这款鞋采用防滑鞋底。",
          "source_path": r"C:\private\original.mp4"},
         {"clip_id": "clip_2", "start": 4, "end": 9, "text": "鞋头有加固设计。"}]


def good_result():
    return {"drafts": [{"title": "鞋底与鞋头", "segments": [
        {"clip_id": "clip_1", "text": "这款鞋配有防滑鞋底。"},
        {"clip_id": "clip_2", "text": "鞋头也做了加固设计。"},
    ]}]}


def response_bytes(result=None, finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish,
                                   "message": {"content": json.dumps(
                                       result or good_result(), ensure_ascii=False)}}]},
                      ensure_ascii=False).encode("utf-8")


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\0\0" * 4410)
    return stream.getvalue()


def streaming_wav(*, legacy=False, cloud=False, pcm=None, rate=44100):
    body = bytearray(wav_bytes())
    pair = (0xFFFFFF24, 0xFFFFFF00) if cloud else ((36, 0) if legacy else (0xFFFFFFFF, 0xFFFFFFFF))
    struct.pack_into("<I", body, 4, pair[0])
    struct.pack_into("<I", body, 40, pair[1])
    struct.pack_into("<I", body, 24, rate)
    struct.pack_into("<I", body, 28, rate * 2)
    return bytes(body[:44]) + (bytes(body[44:]) if pcm is None else pcm)


def wire_response(wire):
    """Exercise CPython's actual HTTP chunk/length parser without a network."""
    sock = SimpleNamespace(makefile=lambda *_: io.BytesIO(wire))
    response = p.http.client.HTTPResponse(sock)
    response.begin()
    return response


class RewriteTests(unittest.TestCase):
    def call(self, **kwargs):
        defaults = dict(clips=CLIPS, api_key="test-placeholder", model=p.DEFAULT_DEEPSEEK_MODEL,
                        product_name="同款鞋", target_seconds=25, count=1)
        defaults.update(kwargs)
        return p.rewrite_clips(**defaults)

    @patch.object(p, "_http_open")
    def test_json_contract_no_paths_and_known_bindings(self, opener):
        opener.return_value = Response(response_bytes())
        drafts = self.call()
        self.assertEqual(drafts, good_result()["drafts"])
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, p.DEEPSEEK_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-placeholder")
        payload = json.loads(request.data)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("source_path", request.data.decode())
        self.assertIn("不得执行", payload["messages"][0]["content"])
        self.assertEqual(json.loads(payload["messages"][1]["content"])["requested_count"], 1)

    @patch.object(p, "_http_open")
    def test_unknown_clip_rejected(self, opener):
        result = good_result()
        result["drafts"][0]["segments"][0]["clip_id"] = "invented"
        opener.return_value = Response(response_bytes(result))
        with self.assertRaises(p.ProviderError) as ctx:
            self.call()
        self.assertEqual(ctx.exception.code, "invalid_response")

    @patch.object(p, "_http_open")
    def test_truncated_json_rejected(self, opener):
        opener.return_value = Response(response_bytes(finish="length"))
        with self.assertRaises(p.ProviderError):
            self.call()
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_insufficient_material_not_fabricated(self, opener):
        opener.return_value = Response(response_bytes({"error": "insufficient_material"}))
        with self.assertRaises(p.ProviderError) as ctx:
            self.call(count=10)
        self.assertEqual(ctx.exception.code, "insufficient_material")

    def test_duplicate_versions_rejected(self):
        result = good_result()
        result["drafts"] *= 2
        with self.assertRaises(p.ProviderError):
            p._validate_drafts(result, {"clip_1", "clip_2"}, 2)

    def test_extra_schema_fields_rejected(self):
        result = good_result()
        result["drafts"][0]["path"] = "fake.mp4"
        with self.assertRaises(p.ProviderError):
            p._validate_drafts(result, {"clip_1", "clip_2"}, 1)

    def test_repeated_clip_within_one_draft_rejected(self):
        result = good_result()
        result["drafts"][0]["segments"][1]["clip_id"] = "clip_1"
        with self.assertRaises(p.ProviderError) as ctx:
            p._validate_drafts(result, {"clip_1", "clip_2"}, 1)
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_renderer_segment_limit_shared_by_provider(self):
        segments = [{"clip_id": f"c{i}", "text": f"说明{i}。"} for i in range(65)]
        known = {s["clip_id"] for s in segments}
        result = {"drafts": [{"title": "上限测试", "segments": segments}]}
        with self.assertRaises(p.ProviderError):
            p._validate_drafts(result, known, 1)
        result["drafts"][0]["segments"] = segments[:64]
        self.assertEqual(len(p._validate_drafts(result, known, 1)[0]["segments"]), 64)

    @patch.object(p, "_http_open")
    def test_bad_inputs_do_not_spend(self, opener):
        for kwargs in ({"count": True}, {"count": 0}, {"count": 21},
                       {"target_seconds": float("nan")}, {"target_seconds": 0},
                       {"api_key": "bad\nheader"}, {"clips": []},
                       {"clips": CLIPS + [CLIPS[0]]}, {"model": "../anything"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(p.ProviderError):
                self.call(**kwargs)
        opener.assert_not_called()

    @patch.object(p, "_http_open")
    def test_timeout_not_automatically_retried_or_exposed(self, opener):
        opener.side_effect = urllib.error.URLError("secret-detail")
        with self.assertRaises(p.ProviderError) as ctx:
            self.call()
        self.assertTrue(ctx.exception.result_unknown)
        self.assertNotIn("secret-detail", str(ctx.exception))
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_http_auth_and_rate_errors_sanitized(self, opener):
        for code in (401, 402, 429, 503):
            opener.side_effect = urllib.error.HTTPError(
                p.DEEPSEEK_URL, code, "private-body", {}, io.BytesIO(b"private-body"))
            with self.subTest(code=code), self.assertRaises(p.ProviderError) as ctx:
                self.call()
            self.assertEqual(ctx.exception.code, f"http_{code}")
            self.assertNotIn("private", str(ctx.exception))
            self.assertEqual(ctx.exception.result_unknown, code == 503)

    @patch.object(p, "_http_open")
    def test_pre_cancel_never_submits(self, opener):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(p.ProviderError) as ctx:
            self.call(cancel=cancel)
        self.assertEqual(ctx.exception.code, "cancelled")
        opener.assert_not_called()

    @patch.object(p, "_http_open")
    def test_response_size_limited(self, opener):
        opener.return_value = Response(b"x" * 101)
        with self.assertRaises(p.ProviderError) as ctx:
            p._post(p.DEEPSEEK_URL, {}, "test-placeholder", maximum=100)
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_redirect_credentials_not_forwarded(self):
        self.assertIsNone(p._NoRedirect().redirect_request(None, None, 307, "", {},
                                                         "https://different.example"))


class FishTests(unittest.TestCase):
    @patch.object(p, "_http_open")
    def test_known_streaming_wav_headers_are_normalized_without_another_post(self, opener):
        for kind in ("legacy", "unknown", "cloud"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                opener.reset_mock()
                opener.return_value = Response(
                    streaming_wav(legacy=kind == "legacy", cloud=kind == "cloud"), "audio/wav")
                output = Path(tmp) / "voice.wav"
                result = p.synthesize_voice("测试", output, api_key="test-placeholder")
                self.assertEqual(output.read_bytes(), wav_bytes())
                self.assertAlmostEqual(result["duration"], .1)
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_chunked_streaming_wav_split_header_uses_real_http_parser(self, opener):
        audio = streaming_wav()
        chunks = [audio[:7], audio[7:31], audio[31:]]
        wire = b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nTransfer-Encoding: chunked\r\n\r\n"
        wire += b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
        opener.return_value = wire_response(wire)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            result = p.synthesize_voice("测试", output, api_key="test-placeholder")
            self.assertAlmostEqual(result["duration"], .1)
            self.assertEqual(output.read_bytes(), wav_bytes())
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_streaming_wav_rejects_bad_fmt_empty_and_partial_frames(self, opener):
        variants = [streaming_wav(pcm=b""), streaming_wav(pcm=b"\0" * 3)]
        for offset, fmt, value in ((16, "<I", 18), (20, "<H", 3), (22, "<H", 0),
                                   (22, "<H", 2), (24, "<I", 0), (28, "<I", 1),
                                   (32, "<H", 1), (34, "<H", 8)):
            body = bytearray(streaming_wav())
            struct.pack_into(fmt, body, offset, value)
            variants.append(bytes(body))
        for index, audio in enumerate(variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                opener.reset_mock()
                opener.return_value = Response(audio, "audio/wav")
                output = Path(tmp) / "voice.wav"
                with self.assertRaises(p.ProviderError) as ctx:
                    p.synthesize_voice("测试", output, api_key="test-placeholder")
                self.assertEqual(ctx.exception.code, "invalid_audio")
                self.assertFalse(output.exists())
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_ordinary_truncated_or_unrecognized_length_headers_are_not_repaired(self, opener):
        variants = [wav_bytes()[:-2]]
        for riff, data in ((36, 0xFFFFFFFF), (0xFFFFFFFF, 0), (0, 0),
                           (0xFFFFFF26, 0xFFFFFF02), (0xFFFFFF24, 0xFFFFFF01),
                           (len(wav_bytes()) - 8, 0), (len(wav_bytes()) - 8, 10000)):
            body = bytearray(wav_bytes())
            struct.pack_into("<I", body, 4, riff)
            struct.pack_into("<I", body, 40, data)
            variants.append(bytes(body))
        for index, audio in enumerate(variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                opener.reset_mock()
                opener.return_value = Response(audio, "audio/wav")
                output = Path(tmp) / "voice.wav"
                with self.assertRaises(p.ProviderError):
                    p.synthesize_voice("测试", output, api_key="test-placeholder")
                self.assertFalse(output.exists())
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_streaming_wav_duration_over_limit_rejected(self, opener):
        opener.return_value = Response(streaming_wav(pcm=b"\0\0" * (8000 * 601), rate=8000),
                                         "audio/wav")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            with self.assertRaises(p.ProviderError):
                p.synthesize_voice("测试", output, api_key="test-placeholder")
            self.assertFalse(output.exists())
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_voice_id_wav_request_and_measured_duration(self, opener):
        opener.return_value = Response(wav_bytes(), "audio/wav")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            result = p.synthesize_voice("这款鞋的鞋头有加固设计。", output,
                                        api_key="test-placeholder", voice_id=p.DEFAULT_VOICE_ID)
            self.assertAlmostEqual(result["duration"], 0.1)
            self.assertEqual(output.read_bytes(), wav_bytes())
            request = opener.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["reference_id"], p.DEFAULT_VOICE_ID)
            self.assertEqual(payload["format"], "wav")
            self.assertEqual(request.get_header("Model"), p.FISH_MODEL)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-placeholder")

    @patch.object(p, "_http_open")
    def test_existing_voice_not_overwritten_or_charged(self, opener):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            output.write_bytes(b"keep")
            with self.assertRaises(p.ProviderError) as ctx:
                p.synthesize_voice("测试", output, api_key="test-placeholder")
            self.assertEqual(ctx.exception.code, "output_exists")
            self.assertEqual(output.read_bytes(), b"keep")
        opener.assert_not_called()

    @patch.object(p, "_http_open")
    def test_invalid_audio_deletes_only_own_partial(self, opener):
        opener.return_value = Response(b"not audio" * 10, "application/json")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            with self.assertRaises(p.ProviderError):
                p.synthesize_voice("测试", output, api_key="test-placeholder")
            self.assertFalse(output.exists())

    @patch.object(p, "_http_open")
    def test_cancel_after_submission_reports_unknown(self, opener):
        cancel = threading.Event()
        def open_and_cancel(*args):
            cancel.set()
            return Response(wav_bytes(), "audio/wav")
        opener.side_effect = open_and_cancel
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            with self.assertRaises(p.ProviderError) as ctx:
                p.synthesize_voice("测试", output, api_key="test-placeholder", cancel=cancel)
            self.assertTrue(ctx.exception.result_unknown)
            self.assertFalse(output.exists())
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_voice_bad_controls_and_paths_not_sent(self, opener):
        with tempfile.TemporaryDirectory() as tmp:
            for text, output, voice in (
                ("<|speaker:1|>测试", Path(tmp) / "a.wav", p.DEFAULT_VOICE_ID),
                ("测试", Path("relative.wav"), p.DEFAULT_VOICE_ID),
                ("测试", Path(tmp) / "b.wav", "http://bad"),
            ):
                with self.subTest(output=output), self.assertRaises(p.ProviderError):
                    p.synthesize_voice(text, output, api_key="test-placeholder", voice_id=voice)
        opener.assert_not_called()


class PaidHTTPFramingTests(unittest.TestCase):
    def post(self, **kwargs):
        return p._post(p.FISH_URL, {}, "test-placeholder", require_framing=True, **kwargs)

    @patch.object(p, "_http_open")
    def test_partial_and_no_content_statuses_are_not_complete_audio(self, opener):
        for status in (204, 206):
            with self.subTest(status=status):
                opener.reset_mock()
                opener.return_value = wire_response(
                    f"HTTP/1.1 {status} Partial\r\nContent-Length: 3\r\n\r\n".encode() + b"abc")
                with self.assertRaises(p.ProviderError) as ctx:
                    self.post()
                self.assertTrue(ctx.exception.result_unknown)
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_short_content_length_is_unknown_and_never_retried(self, opener):
        opener.return_value = wire_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
        with self.assertRaises(p.ProviderError) as ctx:
            self.post()
        self.assertTrue(ctx.exception.result_unknown)
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_incomplete_chunk_and_missing_terminator_are_unknown(self, opener):
        for wire in (b"a\r\nshort", b"5\r\nshort\r\n"):
            with self.subTest(wire=wire):
                opener.reset_mock()
                opener.return_value = wire_response(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + wire)
                with self.assertRaises(p.ProviderError) as ctx:
                    self.post()
                self.assertTrue(ctx.exception.result_unknown)
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_invalid_or_ambiguous_framing_rejected_before_reading(self, opener):
        for headers in (
            b"", b"Content-Length: -1\r\n", b"Content-Length: 3, 3\r\n",
            b"Content-Length: nonsense\r\n", b"Content-Length: 3\r\nContent-Length: 4\r\n",
            b"Content-Length: 3\r\nTransfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: gzip, chunked\r\n",
        ):
            with self.subTest(headers=headers):
                opener.reset_mock()
                opener.return_value = wire_response(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\nabc")
                with self.assertRaises(p.ProviderError) as ctx:
                    self.post()
                self.assertTrue(ctx.exception.result_unknown)
                self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_chunked_header_without_actual_http_chunk_decoder_is_rejected(self, opener):
        response = Response(b"body")
        response.headers.pop("Content-Length")
        response.headers["Transfer-Encoding"] = "chunked"
        opener.return_value = response
        with self.assertRaises(p.ProviderError) as ctx:
            self.post()
        self.assertTrue(ctx.exception.result_unknown)
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_close_delimited_deepseek_default_stays_compatible(self, opener):
        response = Response(response_bytes())
        response.headers.pop("Content-Length")
        opener.return_value = response
        raw, _ = p._post(p.DEEPSEEK_URL, {}, "test-placeholder")
        self.assertEqual(raw, response_bytes())
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_http_open")
    def test_unframed_or_short_wav_is_not_saved_even_if_pcm_looks_complete(self, opener):
        for length in (None, len(streaming_wav()) + 2):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                opener.reset_mock()
                response = Response(streaming_wav(), "audio/wav")
                response.headers.pop("Content-Length")
                if length is not None:
                    response.headers["Content-Length"] = str(length)
                opener.return_value = response
                output = Path(tmp) / "voice.wav"
                with self.assertRaises(p.ProviderError) as ctx:
                    p.synthesize_voice("测试", output, api_key="test-placeholder")
                self.assertTrue(ctx.exception.result_unknown)
                self.assertFalse(output.exists())
                self.assertEqual(opener.call_count, 1)


class ASRTests(unittest.TestCase):
    @patch.object(p, "_require_module")
    @patch.object(p, "_run_process")
    def test_isolated_asr_offline_and_timed(self, run, _require):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            source.write_bytes(b"fixture")
            model = root / "model"
            model.mkdir()
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (model / name).write_bytes(b"fixture")
            def emit(args, **kwargs):
                self.assertIn("-I", args)
                self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
                output = Path(args[-1])
                output.write_text(json.dumps([{"start": 0, "end": 2.5, "text": "原文"}]),
                                  encoding="utf-8")
                return 0, "ASR_LOCAL_COMPLETE"
            run.side_effect = emit
            self.assertEqual(p.transcribe_video(source, str(model)),
                             [{"start": 0.0, "end": 2.5, "text": "原文"}])

    @patch.object(p, "_run_process")
    def test_model_id_not_downloaded(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"fixture")
            with self.assertRaises(p.ProviderError) as ctx:
                p.transcribe_video(source, "large-v3")
        self.assertEqual(ctx.exception.code, "missing_model")
        run.assert_not_called()

    def test_bad_timestamps_and_empty_asr(self):
        for value in ([], [{"start": 1, "end": 0, "text": "wrong"}],
                      [{"start": float("nan"), "end": 1, "text": "wrong"}]):
            with self.subTest(value=value), self.assertRaises((ValueError, p.ProviderError)):
                p._validate_transcript(value)

    def test_real_subprocess_cancelled(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            with self.assertRaises(p.ProviderError) as ctx:
                p._run_process([p.sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                               cancel=cancel, timeout=5)
            self.assertEqual(ctx.exception.code, "cancelled")
        finally:
            timer.cancel()

    def test_real_subprocess_timeout(self):
        with self.assertRaises(p.ProviderError) as ctx:
            p._run_process([p.sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                           timeout=0.1)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_worker_constructs_local_cpu_model_and_writes_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"fixture")
            model = root / "model"
            model.mkdir()
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (model / name).write_bytes(b"fixture")
            output = root / "transcript.json"
            fake = Mock()
            fake.return_value.transcribe.return_value = (
                iter([SimpleNamespace(start=0.1, end=2.3, text=" 原文 ")]), SimpleNamespace())
            module = SimpleNamespace(WhisperModel=fake)
            with patch.dict(p.sys.modules, faster_whisper=module), patch("sys.stdout", new=io.StringIO()):
                code = asr_worker.main(["--source", str(source), "--model", str(model),
                                        "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(fake.call_args.kwargs["device"], "cpu")
            self.assertEqual(fake.call_args.kwargs["compute_type"], "int8")
            self.assertTrue(fake.call_args.kwargs["local_files_only"])
            self.assertEqual(fake.call_args.kwargs["num_workers"], 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")),
                             [{"start": 0.1, "end": 2.3, "text": "原文"}])

    def test_worker_existing_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "keep.json"
            output.write_bytes(b"keep")
            with patch("sys.stderr", new=io.StringIO()):
                code = asr_worker.main(["--source", str(root / "missing.mp4"),
                                        "--model", str(root), "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(output.read_bytes(), b"keep")


class DownloadTests(unittest.TestCase):
    def test_approved_url_and_share_text(self):
        self.assertEqual(p.normalize_douyin_url("分享 https://v.douyin.com/abcDEF/ 复制链接"),
                         "https://v.douyin.com/abcDEF/")
        self.assertEqual(p.normalize_douyin_url("https://www.douyin.com/video/123456789?x=1"),
                         "https://www.douyin.com/video/123456789")

    def test_other_hosts_profiles_credentials_rejected(self):
        for url in ("https://www.douyin.com/user/abc", "https://evil.example/video/12345",
                    "https://www.douyin.com.evil.example/video/12345",
                    "https://u:p@www.douyin.com/video/12345",
                    "http://v.douyin.com/ABCdef", "https://www.douyin.com/collection/12345",
                    "https://v.douyin.com/AAA https://v.douyin.com/BBB",
                    "https://v.douyin.com:123/abc", "https://www.douyin.com/note/12345"):
            with self.subTest(url=url), self.assertRaises(p.ProviderError):
                p.normalize_douyin_url(url)

    @patch.object(p, "_public_download_addresses", return_value=[])
    @patch.object(p, "_open_public_download")
    def test_share_redirect_resolves_video_only(self, opener, _dns):
        opener.return_value = DownloadResponse(status=302, headers={
            "Location": "https://www.iesdouyin.com/share/video/123456789/?x=1"})
        self.assertEqual(p._resolve_douyin_video_url("https://v.douyin.com/AbCdEf/"),
                         "https://www.douyin.com/video/123456789")
        self.assertEqual(opener.call_args.kwargs["method"], "HEAD")
        self.assertTrue(opener.return_value.closed)

    @patch.object(p, "_public_download_addresses", return_value=[])
    @patch.object(p, "_open_public_download")
    def test_share_profile_redirect_rejected_before_download(self, opener, _dns):
        opener.return_value = DownloadResponse(status=302, headers={
            "Location": "https://www.douyin.com/user/somebody"})
        with self.assertRaises(p.ProviderError) as ctx:
            p._resolve_douyin_video_url("https://v.douyin.com/AbCdEf/")
        self.assertEqual(ctx.exception.code, "invalid_url")
        self.assertEqual(opener.call_count, 1)

    @patch.object(p, "_public_download_addresses", return_value=[])
    @patch.object(p, "_open_public_download")
    def test_share_external_redirect_not_followed(self, opener, _dns):
        opener.return_value = DownloadResponse(status=302, headers={
            "Location": "http://127.0.0.1:123/private"})
        with self.assertRaises(p.ProviderError):
            p._resolve_douyin_video_url("https://v.douyin.com/AbCdEf/")
        self.assertEqual(opener.call_count, 1)


class DownloadResponse:
    """Network fixture; no real requests or login-state access in these tests."""
    def __init__(self, body=b"", *, status=200, headers=None, on_read=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.closed = False
        self.on_read = on_read

    def read1(self, size):
        if self.on_read:
            self.on_read()
        return self.body.read(size)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class MobileFeedDownloadTests(unittest.TestCase):
    video_id = "123456789"
    media_url = "https://v5-hl-zenl-ov.zjcdn.com/media.mp4?signature=do-not-log"
    mp4 = b"\0\0\0\x18ftypisom\0\0\0\0isomiso2" + b"fixture-media"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.patchers = [
            patch.object(p, "_public_download_addresses", return_value=[]),
            patch.object(p, "_open_public_download"),
            patch.object(p.shutil, "which", side_effect=lambda name: name),
            patch.object(p, "_run_process", side_effect=self.fake_media_process),
        ]
        self.dns, self.opener, self.which, self.run = [x.start() for x in self.patchers]
        for x in self.patchers:
            self.addCleanup(x.stop)

    @staticmethod
    def fake_media_process(args, **_):
        if "-show_streams" in args:
            return 0, json.dumps({
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.0"},
                "streams": [{"codec_type": "video", "width": 720, "height": 1280},
                            {"codec_type": "audio"}],
            })
        return 0, ""

    def metadata(self, *, video_id=None, urls=None):
        return {"status_code": 0, "aweme_list": [{
            "aweme_id": video_id or self.video_id, "desc": "同款鞋",
            "video": {"duration": 5000, "play_addr": {"url_list": urls or [self.media_url]}},
        }]}

    def enqueue(self, metadata=None, media=None, headers=None):
        self.opener.side_effect = [
            DownloadResponse(json.dumps(metadata or self.metadata()).encode()),
            DownloadResponse(self.mp4 if media is None else media, headers=headers),
        ]

    def download(self, **kwargs):
        return p.download_douyin(f"https://www.douyin.com/video/{self.video_id}",
                                 self.folder, **kwargs)

    def assert_empty(self):
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_feed_exact_query_native_https_and_validated_mp4(self):
        self.enqueue()
        result = self.download()
        self.assertEqual(Path(result["path"]).read_bytes(), self.mp4)
        self.assertEqual(result["title"], "同款鞋")
        self.assertIn("预览", result["warning"])
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        calls = self.opener.call_args_list
        self.assertEqual(calls[0].args[0],
                         "https://aweme.snssdk.com/aweme/v1/feed/?aweme_id=123456789&aid=1128")
        self.assertEqual(calls[1].args[0], self.media_url)
        self.assertEqual(self.run.call_count, 2)
        self.assertIn("-xerror", self.run.call_args.args[0])
        self.assertIn("-protocol_whitelist", self.run.call_args.args[0])

    def test_wrong_video_id_never_downloaded(self):
        self.enqueue(self.metadata(video_id="999999999"))
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertEqual(self.opener.call_count, 1)
        self.run.assert_not_called()
        self.assert_empty()

    def test_selects_exact_id_not_first_feed_item(self):
        meta = self.metadata()
        meta["aweme_list"].insert(0, self.metadata(video_id="999999999")["aweme_list"][0])
        self.enqueue(meta)
        self.assertTrue(Path(self.download()["path"]).is_file())

    def test_duplicate_matching_ids_rejected(self):
        meta = self.metadata()
        meta["aweme_list"] *= 2
        self.enqueue(meta)
        with self.assertRaises(p.ProviderError):
            self.download()
        self.assertEqual(self.opener.call_count, 1)
        self.assert_empty()

    def test_unreviewed_or_plain_http_media_not_requested(self):
        for url in ("https://unreviewed.zjcdn.com/video.mp4",
                    "https://v5-hl-zenl-ov.zjcdn.com.evil.test/video",
                    "http://v5-hl-zenl-ov.zjcdn.com/video.mp4",
                    "https://user:pass@v5-hl-zenl-ov.zjcdn.com/video"):
            with self.subTest(url=url):
                self.enqueue(self.metadata(urls=[url]))
                with self.assertRaises(p.ProviderError):
                    self.download()
                self.assert_empty()
        self.run.assert_not_called()

    def test_native_https_candidate_selected_without_rewriting(self):
        self.enqueue(self.metadata(urls=["http://v5-hl-zenl-ov.zjcdn.com/old",
                                        self.media_url]))
        self.download()
        self.assertEqual(self.opener.call_args.args[0], self.media_url)

    def test_media_redirect_validated_before_next_request(self):
        for target in ("https://evil.test/signed?token=do-not-log",
                       "http://v5-hl-zenl-ov.zjcdn.com/no-tls",
                       "https://127.0.0.1/private",
                       "https://v5-hl-zenl-ov.zjcdn.com:444/private"):
            with self.subTest(target=target):
                first = DownloadResponse(json.dumps(self.metadata()).encode())
                redirect = DownloadResponse(status=302, headers={"Location": target})
                self.opener.reset_mock()
                self.opener.side_effect = [first, redirect]
                with self.assertRaises(p.ProviderError) as ctx:
                    self.download()
                self.assertEqual(self.opener.call_count, 2)
                self.assertNotIn("do-not-log", str(ctx.exception))
                self.assertTrue(redirect.closed)
                self.assert_empty()

    def test_relative_public_cdn_redirect_allowed(self):
        self.opener.side_effect = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(status=302, headers={"Location": "/new.mp4"}),
            DownloadResponse(self.mp4),
        ]
        self.download()
        self.assertEqual(self.opener.call_args.args[0],
                         "https://v5-hl-zenl-ov.zjcdn.com/new.mp4")
        self.assertGreaterEqual(self.dns.call_count, 3)

    def test_redirect_loop_bounded(self):
        self.opener.side_effect = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            *[DownloadResponse(status=302, headers={"Location": "/again.mp4"})
              for _ in range(8)],
        ]
        with self.assertRaises(p.ProviderError):
            self.download()
        self.assertLessEqual(self.opener.call_count, 7)
        self.assert_empty()

    def test_fake_oversize_and_wrong_encoding_cleaned_without_retry(self):
        cases = [(b"<html>error</html>", {}),
                 (self.mp4, {"Content-Length": str(200 * 1024 * 1024 + 1)}),
                 (self.mp4, {"Content-Encoding": "gzip"}),
                 (self.mp4, {"Content-Length": "-1"})]
        for data, headers in cases:
            with self.subTest(headers=headers, data=data[:8]):
                self.enqueue(media=data, headers=headers)
                with self.assertRaises(p.ProviderError):
                    self.download()
                self.assert_empty()

    def test_media_short_read_retries_once_after_cleaning_partial(self):
        progress = Mock()
        responses = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(self.mp4[:10], headers={"Content-Length": str(len(self.mp4))}),
            DownloadResponse(self.mp4, headers={"Content-Length": str(len(self.mp4))}),
        ]
        def open_next(*_, **__):
            if len(responses) == 1:
                self.assert_empty()  # The first .part is already removed.
            return responses.pop(0)
        self.opener.side_effect = open_next
        result = self.download(progress=progress)
        self.assertEqual(Path(result["path"]).read_bytes(), self.mp4)
        self.assertEqual(self.opener.call_count, 3)
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        self.assertTrue(any("重试 1/1" in c.args[1] for c in progress.call_args_list))

    def test_two_media_short_reads_stop_and_clean_both_partials(self):
        self.opener.side_effect = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            *[DownloadResponse(self.mp4[:10], headers={"Content-Length": str(len(self.mp4))})
              for _ in range(2)],
        ]
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertEqual(ctx.exception.code, "download_transport")
        self.assertEqual(self.opener.call_count, 3)
        self.assert_empty()

    def test_cancel_between_transfer_attempts_never_requests_again(self):
        cancel = threading.Event()
        self.opener.side_effect = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(b"", headers={}),
        ]
        def on_progress(_, message):
            if "重试" in message:
                cancel.set()
        with self.assertRaises(p.ProviderError) as ctx:
            self.download(cancel=cancel, progress=on_progress)
        self.assertEqual(ctx.exception.code, "cancelled")
        self.assertEqual(self.opener.call_count, 2)
        self.assert_empty()

    def test_empty_feed_transfer_retried_once_then_media(self):
        self.opener.side_effect = [
            DownloadResponse(b"", headers={"Content-Length": "0"}),
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(self.mp4),
        ]
        self.assertTrue(Path(self.download()["path"]).is_file())
        self.assertEqual(self.opener.call_count, 3)

    def test_connection_transport_error_retried_but_api_error_not_retried(self):
        self.opener.side_effect = [
            p.ProviderError("连接中断", code="download_transport"),
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(self.mp4),
        ]
        self.assertTrue(Path(self.download()["path"]).is_file())
        self.assertEqual(self.opener.call_count, 3)

    def test_stage_retries_share_original_deadline(self):
        self.opener.side_effect = [
            DownloadResponse(),
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(),
            DownloadResponse(self.mp4),
        ]
        self.assertTrue(Path(self.download()["path"]).is_file())
        self.assertEqual(self.opener.call_count, 4)
        self.assertEqual(len({c.kwargs["deadline"] for c in self.opener.call_args_list}), 1)

    def test_two_empty_feed_responses_stop_before_media(self):
        self.opener.side_effect = [DownloadResponse(), DownloadResponse()]
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertEqual(ctx.exception.code, "download_transport")
        self.assertEqual(self.opener.call_count, 2)
        self.run.assert_not_called()
        self.assert_empty()

    def test_streaming_size_limit_without_content_length(self):
        self.enqueue()
        with patch.object(p, "MAX_DOWNLOAD_BYTES", 16), self.assertRaises(p.ProviderError):
            self.download()
        self.assert_empty()

    def test_cancel_during_body_cleans_partial(self):
        cancel = threading.Event()
        self.opener.side_effect = [
            DownloadResponse(json.dumps(self.metadata()).encode()),
            DownloadResponse(self.mp4, on_read=cancel.set),
        ]
        with self.assertRaises(p.ProviderError) as ctx:
            self.download(cancel=cancel)
        self.assertEqual(ctx.exception.code, "cancelled")
        self.run.assert_not_called()
        self.assert_empty()

    def test_pre_cancel_does_not_request(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(p.ProviderError):
            self.download(cancel=cancel)
        self.opener.assert_not_called()

    def test_decode_failure_cleans_partial(self):
        self.enqueue()
        probe = self.fake_media_process(["-show_streams"])
        self.run.side_effect = [probe, (1, "secret signed URL error")]
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(self.opener.call_count, 2)
        self.assert_empty()

    def test_decodable_video_with_wrong_duration_never_published(self):
        meta = self.metadata()
        meta["aweme_list"][0]["video"]["duration"] = 60000
        self.enqueue(meta)
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertEqual(ctx.exception.code, "invalid_download")
        self.assertEqual(self.run.call_count, 1)  # Probe rejects before decode/commit.
        self.assert_empty()

    def test_missing_audio_invalid_duration_or_container_rejected(self):
        for data in (
            {"streams": [{"codec_type": "video"}],
             "format": {"format_name": "mov,mp4", "duration": "5"}},
            {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
             "format": {"format_name": "mov,mp4", "duration": "601"}},
            {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
             "format": {"format_name": "matroska", "duration": "5"}},
        ):
            with self.subTest(data=data):
                self.enqueue()
                self.run.side_effect = None
                self.run.return_value = (0, json.dumps(data))
                with self.assertRaises(p.ProviderError):
                    self.download()
                self.assert_empty()

    def test_existing_final_never_overwritten(self):
        self.enqueue()
        kept = self.folder / f"douyin_{self.video_id}_{'a' * 12}.mp4"
        kept.write_bytes(b"keep")
        with patch.object(p.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)):
            with self.assertRaises(p.ProviderError):
                self.download()
        self.assertEqual(kept.read_bytes(), b"keep")
        self.assertEqual(list(self.folder.iterdir()), [kept])

    def test_metadata_api_failure_sanitized(self):
        self.enqueue({"status_code": 1, "status_msg": "secret signed URL",
                      "aweme_list": []})
        with self.assertRaises(p.ProviderError) as ctx:
            self.download()
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(self.opener.call_count, 1)
        self.assert_empty()


class DownloadNetworkTests(unittest.TestCase):
    def test_tls_verification_error_never_retries_other_addresses(self):
        address = (p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        connection = Mock()
        connection.request.side_effect = p.ssl.SSLCertVerificationError("certificate error")
        with patch.object(p, "_PinnedDownloadHTTPS", return_value=connection) as constructor:
            with self.assertRaises(p.ProviderError) as ctx:
                p._open_public_download(
                    "https://aweme.snssdk.com/aweme/v1/feed/", [address] * 4,
                    method="GET", user_agent=p.DOWNLOAD_APP_UA, cancel=None,
                    deadline=p.time.monotonic() + 10)
            self.assertEqual(ctx.exception.code, "download_tls")
            self.assertEqual(constructor.call_count, 1)
            connection.close.assert_called_once()

    def test_many_unreachable_ipv6_records_do_not_hide_ipv4(self):
        ipv6 = (p.socket.AF_INET6, p.socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 443))
        ipv4 = (p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        connection = Mock()
        connection.getresponse.return_value = DownloadResponse()
        with patch.object(p, "_PinnedDownloadHTTPS", return_value=connection) as constructor:
            response = p._open_public_download(
                "https://aweme.snssdk.com/aweme/v1/feed/", [ipv6] * 6 + [ipv4],
                method="GET", user_agent=p.DOWNLOAD_APP_UA, cancel=None,
                deadline=p.time.monotonic() + 10)
            self.assertEqual(constructor.call_args.args[1], ipv4)
            response.close()

    def test_address_family_fallback_remains_bounded(self):
        ipv6 = (p.socket.AF_INET6, p.socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 443))
        ipv4 = (p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        connection = Mock()
        connection.request.side_effect = OSError("network")
        with patch.object(p, "_PinnedDownloadHTTPS", return_value=connection) as constructor:
            with self.assertRaises(p.ProviderError):
                p._open_public_download(
                    "https://aweme.snssdk.com/aweme/v1/feed/", [ipv6] * 6 + [ipv4] * 6,
                    method="GET", user_agent=p.DOWNLOAD_APP_UA, cancel=None,
                    deadline=p.time.monotonic() + 10)
            self.assertEqual(constructor.call_count, 4)
            self.assertEqual([c.args[1][0] for c in constructor.call_args_list],
                             [p.socket.AF_INET, p.socket.AF_INET6] * 2)

    def test_dns_rejects_private_and_mixed_answers(self):
        for ips in (["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["::1"],
                    ["100.64.0.1"], ["224.0.0.1"], ["fe80::1"], ["ff02::1"],
                    ["::ffff:127.0.0.1"], ["2002:7f00:1::"], ["8.8.8.8", "192.168.1.2"]):
            answers = [(p.socket.AF_INET6 if ":" in ip else p.socket.AF_INET,
                        p.socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
            with self.subTest(ips=ips), patch.object(p.socket, "getaddrinfo", return_value=answers):
                with self.assertRaises(p.ProviderError) as ctx:
                    p._public_download_addresses("v5-hl-zenl-ov.zjcdn.com",
                                                 cancel=None, deadline=p.time.monotonic() + 10)
                self.assertEqual(ctx.exception.code, "invalid_url")

    def test_dns_wait_is_cancellable_and_time_bounded(self):
        for mode in ("cancel", "timeout"):
            with self.subTest(mode=mode):
                finished = threading.Event()
                cancel = threading.Event()
                def blocking_dns(*_, **__):
                    finished.wait(2)
                    return []
                timer = threading.Timer(0.03, cancel.set)
                if mode == "cancel":
                    timer.start()
                try:
                    with patch.object(p.socket, "getaddrinfo", side_effect=blocking_dns):
                        with self.assertRaises(p.ProviderError) as ctx:
                            p._public_download_addresses(
                                "aweme.snssdk.com", cancel=cancel,
                                deadline=p.time.monotonic() + (2 if mode == "cancel" else 0.03))
                    self.assertEqual(ctx.exception.code, "cancelled" if mode == "cancel" else "timeout")
                finally:
                    timer.cancel()
                    finished.set()

    def test_cdn_redirect_to_private_dns_never_opened(self):
        public = [(p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        private = [(p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(p.socket, "getaddrinfo", side_effect=[public, private]), \
                patch.object(p, "_open_public_download", return_value=DownloadResponse(
                    status=302, headers={"Location": "https://v5-dy-ov-experiment.zjcdn.com/x"}
                )) as opener:
            with self.assertRaises(p.ProviderError):
                p._follow_download_redirects(
                    "https://v5-hl-zenl-ov.zjcdn.com/video.mp4", p.DOWNLOAD_MEDIA_HOSTS,
                    cancel=None, deadline=p.time.monotonic() + 10, user_agent=p.DOWNLOAD_MEDIA_UA)
            self.assertEqual(opener.call_count, 1)

    def test_pinned_tls_uses_numeric_address_original_sni_and_no_cookies(self):
        address = (p.socket.AF_INET, p.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        raw_socket, context = Mock(), Mock()
        with patch.object(p.socket, "socket", return_value=raw_socket), \
                patch.object(p.ssl, "create_default_context", return_value=context):
            connection = p._PinnedDownloadHTTPS("aweme.snssdk.com", address, timeout=5)
            connection.connect()
            raw_socket.connect.assert_called_once_with(("8.8.8.8", 443))
            self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"],
                             "aweme.snssdk.com")
        response = DownloadResponse()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(p, "_PinnedDownloadHTTPS", return_value=connection):
            opened = p._open_public_download(
                "https://aweme.snssdk.com/aweme/v1/feed/?aweme_id=12345&aid=1128",
                [address], method="GET", user_agent=p.DOWNLOAD_APP_UA,
                cancel=None, deadline=p.time.monotonic() + 10)
            headers = connection.request.call_args.kwargs["headers"]
            self.assertFalse({"cookie", "authorization", "proxy-authorization"} &
                             {key.lower() for key in headers})
            self.assertEqual(headers["Accept-Encoding"], "identity")
            opened.close()
            connection.close.assert_called_once()


class DownloadRealMediaTests(unittest.TestCase):
    """Synthetic local media + real FFmpeg; still no external network."""
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = p.shutil.which("ffmpeg"), p.shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg and FFprobe are required for real media validation")

    def make_video(self, folder):
        path = folder / "synthetic.mp4"
        code, _ = p._run_process([
            self.ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=72x128:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
            "-t", "1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", "-n", str(path),
        ], timeout=20)
        self.assertEqual(code, 0)
        return path

    def test_real_synthetic_mp4_download_passes_probe_and_full_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_video(root)
            output = root / "output"
            output.mkdir()
            media = original.read_bytes()
            meta = MobileFeedDownloadTests().metadata()
            meta["aweme_list"][0]["video"]["duration"] = 1200
            responses = [
                DownloadResponse(json.dumps(meta).encode()),
                DownloadResponse(media, headers={"Content-Length": str(len(media))}),
            ]
            with patch.object(p, "_public_download_addresses", return_value=[]), \
                    patch.object(p, "_open_public_download", side_effect=responses):
                result = p.download_douyin("https://www.douyin.com/video/123456789", output)
            self.assertEqual(Path(result["path"]).read_bytes(), media)
            self.assertEqual(original.read_bytes(), media)
            self.assertEqual(len(list(output.iterdir())), 1)

    def test_real_truncated_mp4_is_not_published_even_without_content_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.make_video(root)
            output = root / "output"
            output.mkdir()
            media = original.read_bytes()
            meta = MobileFeedDownloadTests().metadata()
            meta["aweme_list"][0]["video"]["duration"] = 1200
            responses = [
                DownloadResponse(json.dumps(meta).encode()),
                DownloadResponse(media[:len(media) * 3 // 4]),
            ]
            with patch.object(p, "_public_download_addresses", return_value=[]), \
                    patch.object(p, "_open_public_download", side_effect=responses):
                with self.assertRaises(p.ProviderError) as ctx:
                    p.download_douyin("https://www.douyin.com/video/123456789", output)
            self.assertEqual(ctx.exception.code, "invalid_download")
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

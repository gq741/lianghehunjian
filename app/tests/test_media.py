"""Offline media tests. Integration fixtures are generated, not user footage."""

import array
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from mixstudio import media


def tone(path, seconds, frequency=440, rate=24000):
    data = array.array("h", (int(6000 * math.sin(2 * math.pi * frequency * i / rate))
                            for i in range(round(seconds * rate))))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(data.tobytes())


def command(args):
    result = subprocess.run(args, capture_output=True, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


class MediaUnitTests(unittest.TestCase):
    def test_number_rejects_nonfinite_and_boolean(self):
        for value in [float("nan"), float("inf"), True, -1, "not-number"]:
            with self.subTest(value=value), self.assertRaises(media.MediaError):
                media._number(value, "value", 0, 10)

    def test_path_rejects_network_url_and_newline(self):
        for value in ["https://example.com/a.mp4", "a\nb.mp4"]:
            with self.subTest(value=value), self.assertRaises(media.MediaError):
                media._path(value)

    def test_subtract_preserves_disjoint_ranges(self):
        window = {"source_path": Path("same"), "start": 0, "end": 10}
        remaining = media._subtract(window, [{**window, "start": 2, "end": 4},
                                             {**window, "start": 6, "end": 8}])
        self.assertEqual([(w["start"], w["end"]) for w in remaining], [(0, 2), (4, 6), (8, 10)])

    def test_subtract_does_not_exclude_another_asset(self):
        window = {"source_path": Path("same"), "start": 0, "end": 10}
        self.assertEqual(media._subtract(window, [{**window, "source_path": Path("different")}]),
                         [window])

    def test_ass_text_is_not_executable_override(self):
        text = media._caption_text(r"{\pos(0,0)}literal", 80)
        self.assertNotIn("{", text)
        self.assertNotIn("\\", text)
        self.assertIn("literal", text)

    def test_encoder_presets_are_not_fixed(self):
        self.assertIn("p2", media._encoder_args("nvenc", "fast"))
        self.assertIn("p6", media._encoder_args("nvenc", "quality"))
        self.assertIn("slow", media._encoder_args("cpu", "quality"))

    def test_voice_units_are_measured_not_source_time(self):
        warnings = []
        cues, units = media._voice_units([{"text": "first"}, {"text": "second"}], {
            "subtitle_segments": [{"start": 0, "end": .8, "text": "first"},
                                  {"start": .8, "end": 2, "text": "second"}]}, 2, warnings)
        self.assertEqual(units, [.8, 1.2])
        self.assertEqual(warnings, [])
        self.assertEqual(cues[-1]["end"], 2)

    def test_approximate_caption_timing_is_reported(self):
        warnings = []
        _, units = media._voice_units([{"text": "ab"}, {"text": "c"}], {}, 3, warnings)
        self.assertEqual(units, [2, 1])
        self.assertTrue(warnings)

    def test_bad_cue_order_rejected(self):
        with self.assertRaises(media.MediaError):
            media._voice_units([{}, {}], {"subtitle_segments": [
                {"start": 1, "end": 2}, {"start": .5, "end": 1}]}, 3, [])

    def test_band_stays_inside_source(self):
        for pct in [0, 50, 95]:
            y, height = media._band(480, pct, 45)
            self.assertGreaterEqual(y, 2)
            self.assertLessEqual(y + height, 478)
            self.assertEqual(height % 2, 0)

    def test_cancelled_process_is_terminated(self):
        event = threading.Event()
        timer = threading.Timer(.35, event.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(media.MediaCancelled):
                media._run([sys.executable, "-c", "import time; time.sleep(30)"], cancel=event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 8)

    def test_publish_never_overwrites(self):
        with tempfile.TemporaryDirectory() as name:
            src, dst = Path(name) / "a", Path(name) / "b"
            src.write_text("new")
            dst.write_text("old")
            with self.assertRaises(media.MediaError):
                media._publish(src, dst)
            self.assertEqual(dst.read_text(), "old")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg not installed")
class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="mixstudio-media-test-")
        cls.root = Path(cls.temp.name)
        cls.a, cls.b = cls.root / "素材甲.mp4", cls.root / "素材乙.mp4"
        for dest, color, size, fps in [(cls.a, "red", "320x480", 24),
                                       (cls.b, "blue", "240x240", 25)]:
            command(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n",
                     "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d=3",
                     "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
                     "-vf", "drawbox=x=0:y=ih*0.75:w=iw:h=ih*0.18:color=white:t=fill",
                     "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
                     "-c:a", "aac", "-shortest", dest])
        cls.voice, cls.music = cls.root / "配音.wav", cls.root / "bgm.wav"
        tone(cls.voice, 2.4)
        tone(cls.music, .6, 220)
        cls.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [cls.a, cls.b, cls.voice, cls.music]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def settings(self, **overrides):
        return {
            "width": 320, "height": 480, "fps": 30, "preset": "fast",
            "encoder": "cpu", "transition": "fade", "transition_seconds": .2,
            "clean_mode": "blur", "caption_plate": True, "subtitle_font_size": 20,
            "bgm_path": str(self.music), "bgm_gain": .15,
            "subtitle_segments": [{"start": 0, "end": 1.2, "text": "第一段商品展示"},
                                  {"start": 1.2, "end": 2.4, "text": "第二段细节展示"}],
            **overrides,
        }

    def clips(self):
        return [{"source_path": str(self.a), "start": 0, "end": 2, "text": "第一段商品展示"},
                {"source_path": str(self.b), "start": 0, "end": 2, "text": "第二段细节展示"}]

    def test_probe_video_and_audio(self):
        video, audio = media.probe(self.a), media.probe(self.voice)
        self.assertEqual((video["width"], video["height"]), (320, 480))
        self.assertAlmostEqual(video["duration"], 3, delta=.05)
        self.assertTrue(audio["has_audio"])
        self.assertEqual(audio["width"], 0)

    def test_thumbnail_and_no_overwrite(self):
        path = self.root / "thumb.jpg"
        media.thumbnail(self.a, path)
        self.assertGreater(path.stat().st_size, 100)
        with self.assertRaises(media.MediaError):
            media.thumbnail(self.a, path)

    def test_real_fade_caption_bgm_and_original_audio_excluded(self):
        output = self.root / "fade-caption-bgm.mp4"
        progress = []
        result = media.render_video(self.clips(), self.voice, output, self.settings(),
                                    progress=lambda value, message: progress.append(value))
        self.assertAlmostEqual(result["duration"], 2.4, delta=.07)
        self.assertEqual((result["width"], result["height"]), (320, 480))
        self.assertEqual(result["encoder_used"], "cpu")
        self.assertAlmostEqual(result["transition_seconds"], .2)
        self.assertEqual(progress[-1], 1)
        self.assertEqual(len(result["timeline"]), 2)
        self.assertAlmostEqual(result["timeline"][-1]["output_end"], 2.4, places=5)
        # Read real decoded PCM: replacement voice present, original 880 Hz excluded.
        pcm = command(["ffmpeg", "-v", "error", "-i", output, "-ss", "0.5", "-t", "0.5",
                       "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])
        samples = array.array("h")
        samples.frombytes(pcm)
        def amplitude(hz):
            return abs(sum(v * complex(math.cos(2*math.pi*hz*i/8000),
                                       math.sin(2*math.pi*hz*i/8000))
                           for i, v in enumerate(samples))) / len(samples)
        self.assertGreater(amplitude(440), 100)
        self.assertGreater(amplitude(220), 2)
        self.assertLess(amplitude(880), amplitude(440) * .02)
        # The persistent plate covers the white band, including speech gaps.
        pixels = command(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", output, "-frames:v", "1",
                          "-vf", "crop=2:2:10:390", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        self.assertLess(max(pixels), 90)
        # A transition midpoint contains both red and blue, not an abrupt hard cut.
        pixels = command(["ffmpeg", "-v", "error", "-ss", "1.2", "-i", output, "-frames:v", "1",
                          "-vf", "crop=2:2:140:200", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        self.assertGreater(pixels[0], 20)
        self.assertGreater(pixels[2], 20)
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        if os.environ.get("MIXSTUDIO_MEDIA_TEST_OUTPUT"):
            export = Path(os.environ["MIXSTUDIO_MEDIA_TEST_OUTPUT"])
            export.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, export / "media-proof.mp4")
            command(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", output,
                     "-frames:v", "1", export / "media-proof.png"])

    def test_extend_from_explicit_broll_without_repetition(self):
        output = self.root / "extended.mp4"
        clips = [{"source_path": str(self.a), "start": 0, "end": 1, "text": "补充细节画面",
                  "additional_clips": [{"source_path": str(self.b), "start": 0, "end": 2}]}]
        result = media.render_video(clips, self.voice, output, self.settings(
            clean_mode="none", bgm_path="", subtitle_segments=[
                {"start": 0, "end": 2.4, "text": "补充细节画面"}]))
        self.assertEqual(len(result["timeline"]), 2)
        self.assertGreater(result["timeline"][1]["duration"], 1)
        self.assertAlmostEqual(result["duration"], 2.4, delta=.07)

    def test_insufficient_coverage_is_failure_not_truncated_voice(self):
        output = self.root / "insufficient.mp4"
        clips = [{"source_path": str(self.a), "start": 0, "end": 1, "text": "不足"}]
        with self.assertRaisesRegex(media.MediaError, "补充同商品片段"):
            media.render_video(clips, self.voice, output,
                               self.settings(subtitle_segments=[]))
        self.assertFalse(output.exists())

    def test_same_source_window_not_silently_repeated(self):
        clips = self.clips()
        clips[1]["source_path"] = clips[0]["source_path"]
        with self.assertRaisesRegex(media.MediaError, "重叠复用"):
            media.render_video(clips, self.voice, self.root / "repeat.mp4", self.settings())

    def test_hardware_failure_retries_cpu_and_reports_it(self):
        original_run = media._run
        def fail_nvenc(args, **kwargs):
            if "h264_nvenc" in args:
                raise media.MediaError("Synthetic unavailable encoder")
            return original_run(args, **kwargs)
        with patch.object(media, "_run", side_effect=fail_nvenc):
            result = media.render_video(self.clips(), self.voice, self.root / "fallback.mp4",
                                        self.settings(encoder="nvenc", clean_mode="delogo",
                                                      transition="none"))
        self.assertEqual(result["encoder_used"], "cpu")
        self.assertTrue(any("CPU" in warning for warning in result["warnings"]))

    def test_cancelled_render_never_publishes(self):
        cancel = threading.Event()
        cancel.set()
        output = self.root / "cancelled.mp4"
        with self.assertRaises(media.MediaCancelled):
            media.render_video(self.clips(), self.voice, output, self.settings(), cancel=cancel)
        self.assertFalse(output.exists())

    def test_output_equal_input_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.render_video(self.clips(), self.voice, self.a, self.settings())
        self.assertEqual(hashlib.sha256(self.a.read_bytes()).hexdigest(), self.hashes[self.a])

    def test_false_short_voice_duration_is_rejected(self):
        with self.assertRaisesRegex(media.MediaError, "实际文件不符"):
            media.render_video(self.clips(), self.voice, self.root / "wrong-duration.mp4",
                               self.settings(duration_seconds=1))

    def test_nan_voice_duration_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.render_video(self.clips(), self.voice, self.root / "nan-duration.mp4",
                               self.settings(duration_seconds=float("nan")))


if __name__ == "__main__":
    unittest.main()

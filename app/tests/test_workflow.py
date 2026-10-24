"""Real local scan/render with mocked paid services; never calls a model API."""
from pathlib import Path
from datetime import datetime
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from mixstudio.service import Studio
from mixstudio import media, providers


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class WorkflowTests(unittest.TestCase):
    def wait(self, studio, identifier):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = studio.store.job(identifier)
            if job["status"] in {"completed", "failed", "cancelled"}:
                self.assertEqual(job["status"], "completed", job)
                return job
            time.sleep(.05)
        self.fail("background job timeout")

    def test_material_to_25_second_video_and_cached_voice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = root / "素材"; sources.mkdir()
            studio = Studio(root / "运行数据")
            try:
                studio.settings.update({"output_dir": str(root / "成片"), "width": 320, "height": 480,
                    "fps": 25, "subtitle_font_size": 24, "clean_mode": "blur"})
                for name, color in [("01.mp4", "steelblue"), ("02.mp4", "darkcyan")]:
                    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi",
                        "-i", f"color={color}:s=320x480:r=25:d=15", "-f", "lavfi",
                        "-i", "sine=frequency=880:duration=15", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(sources / name)], check=True, capture_output=True)
                scanned = self.wait(studio, studio.scan(str(sources))["id"])
                self.assertEqual(scanned["result"]["imported"], 2)
                assets = studio.state()["assets"]
                for i, asset in enumerate(assets):
                    studio.set_transcript(asset["id"], [{"start": 0, "end": 7, "text": f"商品展示{i+1}"},
                                                       {"start": 7, "end": 15, "text": f"商品细节{i+1}"}])
                chosen = [studio.store.asset(a["id"])["clips"][0]["clip_id"] for a in assets]
                def rewrite(clips, **kwargs):
                    return [{"title": "合成测试，不是真实广告", "segments": [
                        {"clip_id": chosen[0], "text": "测试第一句，画面取自已选择的素材。"},
                        {"clip_id": chosen[1], "text": "测试第二句，配音由本地测试音模拟。"}]}]
                calls = []
                def synthesize(text, output, **kwargs):
                    calls.append(text)
                    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=12.5", "-ar", "48000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(output)], check=True, capture_output=True)
                    return {"path": str(output), "duration": 12.5}
                with patch.object(studio.settings, "secret", return_value="unit-test-not-a-key"), \
                     patch.object(providers, "rewrite_clips", side_effect=rewrite), \
                     patch.object(providers, "synthesize_voice", side_effect=synthesize), \
                     patch('mixstudio.output_names.datetime') as clock:
                    clock.now.return_value=datetime(2026,1,2,3,4)
                    prepared = self.wait(studio, studio.prepare({"asset_ids": [a["id"] for a in assets],
                        "product_name": "测试商品", "target_seconds": 25, "count": 1, "allow_paid": True})["id"])
                    identifier = prepared["result"]["draft_ids"][0]
                    self.assertEqual(studio.store.draft(identifier)["revision"], 1)
                    with self.assertRaises(ValueError):
                        studio.edit_draft(identifier, [{"clip_id": "foreign", "text": "wrong"}])
                    rendered = self.wait(studio, studio.render(identifier, True)["id"])
                    path = Path(rendered["result"]["path"])
                    self.assertEqual(path.name,'测试商品 1.2.3.mp4')
                    info = media.probe(path)
                    self.assertAlmostEqual(info["duration"], 25, delta=.15)
                    self.assertEqual((info["width"], info["height"]), (320, 480))
                    self.assertEqual(len(calls), 2)
                    # Second intentional export reuses identical completed audio,
                    # but publishes a new unique video, never overwrites.
                    second = self.wait(studio, studio.render(identifier, True)["id"])
                    self.assertEqual(len(calls), 2)
                    self.assertNotEqual(second["result"]["path"], str(path))
                    self.assertEqual(Path(second['result']['path']).name,'测试商品 1.2.3 (2).mp4')
                    self.assertFalse(list((root/'成片').glob('*.reserve')))
                    self.assertTrue(path.is_file())
                    evidence = Path(__file__).resolve().parents[1] / "verification" / "workflow"
                    evidence.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, evidence / "synthetic-25s.mp4")
                    summary = {"kind": "synthetic media + mocked DeepSeek/Fish; NOT real API verification",
                        "duration": info["duration"], "dimensions": [info["width"], info["height"]],
                        "source_count": 2, "voice_requests_mocked": 2, "second_export_voice_cache_hit": True,
                        "warnings": rendered["result"].get("warnings", []), "status": "PASS"}
                    (evidence / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                studio.close()


if __name__ == "__main__":
    unittest.main()

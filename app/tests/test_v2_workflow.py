"""Real V2 local media workflow, with EVERY paid provider mocked.

JD title is a synthetic verified fixture, not a live-page acceptance claim.
FFmpeg scan/probe/concatenation/subtitles/transitions/export are real. DeepSeek
JSON transport, visual scene assessments and Fish sine audio are test doubles.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from mixstudio import media, providers, script_engine, vision
from mixstudio.service import Studio, fingerprint


PRODUCT = {"sku": "100012345678", "title": "测试品牌电视 金属边框",
           "source": "https://item.jd.com/100012345678.html",
           "observed_at": 1792560000, "status": "read"}
LINES = [
    "看看测试品牌电视的整体外观，再留意金属边框的样子，选择时想一想，它放在自己的客厅里会是什么感觉。先看看再说吧。",
    "测试品牌电视有金属边框，这里仅做本地流程验证，画面来自测试素材，配音使用模拟音频，不是实际商品广告。请认真核对。",
]


def ffmpeg(*args):
    command = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n", *map(str, args)]
    return subprocess.run(command, check=True, capture_output=True, timeout=45,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class V2WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mix-v2-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "本地素材"
        self.sources.mkdir()
        self.studio = Studio(self.root / "隔离运行数据")
        self.addCleanup(self.studio.close)
        self.studio.settings.update({"output_dir": str(self.root / "测试成片"), "width": 320,
            "height": 480, "fps": 25, "subtitle_font_size": 24, "preset": "fast",
            "clean_mode": "none", "transition": "fade", "transition_seconds": .2})
        for name, color in (("外观.mp4", "steelblue"), ("边框.mp4", "darkcyan")):
            ffmpeg("-f", "lavfi", "-i", f"color={color}:s=320x480:r=25:d=20",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                   self.sources / name)
        self.source_hashes = {str(path): fingerprint(path) for path in self.sources.glob("*.mp4")}
        self.studio.store.meta("current_product", PRODUCT)
        self.api_calls, self.voice_calls = [], []
        self.voice_seconds = 12.5
        self.lines = LINES
        self.window_seconds = 4
        self.scene_map = {}
        self.api = self.patch(providers, "_post", side_effect=self.mock_completion)
        self.voice = self.patch(providers, "synthesize_voice", side_effect=self.mock_voice)
        self.visual = self.patch(vision, "analyze_asset", side_effect=self.mock_vision)
        self.asr = self.patch(providers, "transcribe_video", side_effect=AssertionError("V2 must never call ASR"))
        self.legacy = self.patch(providers, "rewrite_clips", side_effect=AssertionError("V2 must not use old rewrite flow"))
        self.network = self.patch(providers, "_http_open", side_effect=AssertionError("No real network in workflow test"))
        self.patch(self.studio.settings, "secret", return_value="test-placeholder")
        self.renderer = self.patch(media, "render_video", wraps=media.render_video)

    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def wait(self, job, status="completed"):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            current = self.studio.store.job(job["id"])
            if current["status"] in {"completed", "failed", "cancelled"}:
                self.assertEqual(current["status"], status, current)
                return current
            time.sleep(.025)
        self.fail("V2 background task timed out")

    def mock_completion(self, url, payload, api_key, **kwargs):
        self.assertEqual(url, providers.DEEPSEEK_URL)
        self.assertEqual(api_key, "test-placeholder")
        content = json.loads(payload["messages"][1]["content"])
        if "requested_count" in content:
            self.api_calls.append("generate")
            result = {"drafts": [{"title": "本地合成测试", "voiceover": "".join(self.lines),
                "segments": [{"segment_id": f"s{i+1}", "text": text,
                    "visual_hint": "同型号电视整体与金属边框画面，待匹配",
                    "fact_quotes": ["测试品牌电视", "金属边框"]} for i, text in enumerate(self.lines)]}]}
        else:
            self.api_calls.append("match")
            self.assertEqual(len(content["segments"]), 2)
            self.assertEqual([s["required_seconds"] for s in content["segments"]],
                             [script_engine.estimate_seconds(text) for text in self.lines])
            usable = [s["clip_id"] for s in content["scenes"]]
            self.assertEqual(len(usable), 10)  # Includes two usable but unrelated distractors.
            grouped = list(self.scene_map.values())
            result = {"matches": [{"segment_id": f"s{i+1}",
                       "clip_ids": [s["clip_id"] for s in scenes[:4]],
                       "confidence": .95, "reason": "测试夹具：同商品外观与边框"}
                      for i, scenes in enumerate(grouped)], "unmatched": []}
        envelope = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(result, ensure_ascii=False)}}]}
        return json.dumps(envelope, ensure_ascii=False).encode(), "application/json"

    def mock_vision(self, asset, cache_dir, **kwargs):
        scenes = [{"clip_id": f"{asset['id']}_c{i}", "asset_id": asset["id"],
            "source_path": asset["path"], "start": i * self.window_seconds,
            "end": (i + 1) * self.window_seconds,
            "text": "同商品电视外观与边框" if i < 4 else "无关摆设，不选择",
            "tags": ["电视", "边框"] if i < 4 else ["摆设"],
            "usable": True, "reason": "测试视觉结果，非真实模型验收", "confidence": .95}
            for i in range(5)]
        self.scene_map[asset["id"]] = scenes
        return {"scenes": scenes, "warnings": ["视觉识别是离线测试夹具，不是实际API结果"]}

    def mock_voice(self, text, output, **kwargs):
        self.voice_calls.append(text)
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={self.voice_seconds}",
               "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", output)
        return {"path": str(output), "duration": self.voice_seconds}

    def make_matched_draft(self, target=25):
        if target == 60:
            # A real 57.5-second text estimate and 32 seconds of bound footage
            # per segment pass all early gates. The later 62-second Fish test
            # therefore exercises measured duration, not an earlier rejection.
            self.lines = [text + text + "外观只是选购时需要了解的一部分。" for text in LINES]
            self.window_seconds = 8
            for path in list(self.sources.glob("*.mp4")):
                extended = path.with_suffix(".long.mp4")
                color = "steelblue" if path.name == "外观.mp4" else "darkcyan"
                ffmpeg("-f", "lavfi", "-i", f"color={color}:s=320x480:r=25:d=40",
                       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", extended)
                extended.replace(path)
            self.source_hashes = {str(path): fingerprint(path) for path in self.sources.glob("*.mp4")}
        scanned = self.wait(self.studio.scan(str(self.sources)))
        self.assertEqual(scanned["result"]["imported"], 2)
        assets = self.studio.state()["assets"]
        self.assertTrue(all(not a["clips"] for a in assets))
        generated = self.wait(self.studio.generate_v2({"asset_ids": [a["id"] for a in assets],
            "target_seconds": target, "count": 1, "allow_paid": True}))
        identifier = generated["result"]["draft_ids"][0]
        draft = self.studio.store.draft(identifier)
        self.assertEqual(draft["workflow"], "script_first_v2")
        self.assertEqual(draft["estimated_seconds"], 57.5 if target == 60 else 25)
        self.assertEqual(draft["timing_status"], "ready")
        self.assertEqual(draft["match_status"], "pending")
        self.assertEqual([script_engine.estimate_seconds(s["text"]) for s in draft["segments"]],
                         [28.75, 28.75] if target == 60 else [12.5, 12.5])
        with self.assertRaises(ValueError):
            self.studio.render(identifier, True)
        matched = self.wait(self.studio.match_v2(identifier, True))
        self.assertEqual(matched["result"]["match_status"], "ready")
        draft = self.studio.store.draft(identifier)
        self.assertEqual([len(m["clip_ids"]) for m in draft["matches"]], [4, 4])
        return identifier

    def assert_originals_and_no_network(self):
        self.assertEqual({str(path): fingerprint(path) for path in self.sources.glob("*.mp4")}, self.source_hashes)
        self.asr.assert_not_called()
        self.legacy.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(self.api_calls, ["generate", "match"])

    def test_script_first_real_25_second_export_uses_only_matched_windows_and_cached_voice(self):
        identifier = self.make_matched_draft()
        first = self.wait(self.studio.render(identifier, True))
        path = Path(first["result"]["path"])
        info = media.probe(path)
        self.assertAlmostEqual(info["duration"], 25, delta=.08)
        self.assertEqual((info["width"], info["height"]), (320, 480))
        self.assertTrue(info["has_audio"])
        self.assertEqual(len(self.voice_calls), 2)
        self.assertEqual(first["result"]["voice_duration"], 25)
        self.assertGreater(first["result"]["transition_seconds"], 0)
        render_clips = self.renderer.call_args.args[0]
        self.assertEqual([len(c["additional_clips"]) for c in render_clips], [3, 3])
        for index, unit in enumerate(render_clips):
            matches = self.studio.store.draft(identifier)["matches"][index]["clip_ids"]
            allowed = {c["clip_id"]: c for c in self.studio.store.draft(identifier)["clips"] if c["clip_id"] in matches}
            self.assertEqual(unit["clip_id"], matches[0])
            self.assertEqual([(c["source_path"], c["start"], c["end"]) for c in unit["additional_clips"]],
                             [(allowed[cid]["source_path"], allowed[cid]["start"], allowed[cid]["end"]) for cid in matches[1:]])
        timeline = first["result"]["timeline"]
        self.assertEqual(len(timeline), 8)
        self.assertEqual({t["unit_index"] for t in timeline}, {0, 1})
        self.assertTrue(all(t["end"] <= 16 for t in timeline), "The unrelated 16..20s windows must never be used")
        cached = {p.name: fingerprint(p) for p in (self.studio.root / "voice-cache").glob("*.wav")}
        second = self.wait(self.studio.render(identifier, True))
        self.assertEqual(len(self.voice_calls), 2)
        self.assertNotEqual(second["result"]["path"], str(path))
        self.assertTrue(path.is_file())
        self.assertAlmostEqual(media.probe(Path(second["result"]["path"]))["duration"], 25, delta=.08)
        self.assertEqual({p.name: fingerprint(p) for p in (self.studio.root / "voice-cache").glob("*.wav")}, cached)
        self.assert_originals_and_no_network()

    def test_repeated_plan_rerender_retains_all_candidates_and_original_cache(self):
        identifier=self.make_matched_draft()
        draft=self.studio.store.draft(identifier)
        selected=[{**m,'clip_ids':m['clip_ids'][:3]} for m in draft['match_candidates']]
        saved=self.studio.replace_matches_v2(identifier,selected)
        self.assertEqual(saved['timeline_plan']['repeat_count'],2)
        self.assertEqual(saved['timeline_plan']['speed'],1.)
        first=self.wait(self.studio.render(identifier,True))
        self.assertEqual(first['result']['timeline_plan']['repeat_count'],2)
        cache={p.name:fingerprint(p) for p in (self.studio.root/'voice-cache').glob('*.wav')}
        self.wait(self.studio.render(identifier,True))
        self.assertEqual(len(self.voice_calls),2)
        self.assertEqual(self.studio.store.draft(identifier)['match_candidates'],selected)
        self.assertEqual({p.name:fingerprint(p) for p in (self.studio.root/'voice-cache').glob('*.wav')},cache)
        self.assert_originals_and_no_network()

    def test_short_measured_voice_follows_actual_audio_and_retains_cache(self):
        self.assert_voice_follows_soft_reference_without_new_fish(9.9, 25)

    def assert_voice_follows_soft_reference_without_new_fish(self, segment_seconds, target):
        self.voice_seconds=segment_seconds
        identifier=self.make_matched_draft(target)
        first=self.wait(self.studio.render(identifier,True))
        limit=min(60,2*segment_seconds)
        result=first['result']
        self.assertAlmostEqual(media.probe(Path(result['path']))['duration'],limit,delta=.08)
        self.assertAlmostEqual(result['voice_speed'],2*segment_seconds/limit,places=6)
        self.assertEqual(result['timeline_plan']['phase'],'actual')
        self.assertEqual(result['timeline_plan']['status'],'ready')
        self.assertEqual(len(self.voice_calls),2)
        cache={p.name:fingerprint(p) for p in (self.studio.root/'voice-cache').glob('*.wav')}
        self.assertEqual(len(cache),2)
        self.assertTrue(all(abs(media.probe(p)['duration']-segment_seconds)<.001 for p in (self.studio.root/'voice-cache').glob('*.wav')))
        render_settings=self.renderer.call_args.args[3]
        self.assertEqual(render_settings['min_output_seconds'],.1)
        self.assertEqual(render_settings['max_output_seconds'],60)
        cues=render_settings['subtitle_segments']
        self.assertAlmostEqual(cues[-1]['end'],limit,places=3)
        self.assertAlmostEqual(cues[0]['end'],limit/2,places=3)
        self.wait(self.studio.render(identifier,True))
        self.assertEqual(len(self.voice_calls),2)
        self.assertEqual({p.name:fingerprint(p) for p in (self.studio.root/'voice-cache').glob('*.wav')},cache)
        self.assert_originals_and_no_network()

    def test_reference_deviation_does_not_speed_adequately_covered_audio(self):
        self.assert_voice_follows_soft_reference_without_new_fish(15.1,25)

    def test_above_sixty_second_raw_voice_paced_to_cap_without_new_fish(self):
        self.assert_voice_follows_soft_reference_without_new_fish(31,60)


if __name__ == "__main__":
    unittest.main()

"""Real local FFmpeg proof for transition-only coverage recovery; no model calls."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import media, providers
from mixstudio.timeline import plan_timeline, reuse_stats
import test_media as media_fixtures
import test_v2_workflow as workflow_fixtures


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class TransitionAdaptationMediaTests(unittest.TestCase):
    def test_transition_only_shortage_renders_hard_cuts_without_padding_or_extra_reuse(self):
        with tempfile.TemporaryDirectory(prefix='transition-adaptation-real-') as folder:
            root = Path(folder)
            source, voice, output = root/'moving-source.mp4', root/'local-tone.wav', root/'out.mp4'
            workflow_fixtures.ffmpeg('-f', 'lavfi', '-i',
                'testsrc2=size=160x240:rate=25:duration=3', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', source)
            original_hashes = {source: hashlib.sha256(source.read_bytes()).hexdigest()}
            scene = dict(clip_id='related', source_path=str(source),
                source_sha256=original_hashes[source], start=0., end=3., usable=True)
            segments = [dict(segment_id='s1', text='本地转场覆盖测试')]
            candidates = [dict(segment_id='s1', clip_ids=['related'])]
            with patch.object(providers, '_http_open', side_effect=AssertionError('Unexpected network')) as network:
                plan = plan_timeline(segments, [scene], candidates, target_seconds=9,
                    transition_seconds=.2, voice_seconds=[9.], phase='actual')
                # Drop cosmetic overlap, slow only within the picture bound,
                # and pace speech only if those related source uses still need it.
                self.assertEqual(plan['status'], 'ready', plan)
                self.assertEqual(plan['transition_seconds'], 0.)
                self.assertEqual(plan['transition_adjustment']['reason'], 'transition_overlap_shortage')
                self.assertLessEqual(plan['speed'], 1.5)
                self.assertLessEqual(plan['repeat_count'], 2)
                self.assertEqual(reuse_stats(plan['windows']), {'repeat_count': 1, 'max_uses': 2})
                seconds = plan['total_seconds']
                self.assertAlmostEqual(seconds, 9./plan['speed'])
                self.assertGreaterEqual(plan['video_speed'], .85)
                media_fixtures.tone(voice, seconds)
                original_hashes[voice] = hashlib.sha256(voice.read_bytes()).hexdigest()
                self.assertTrue(all(w['start'] == 0. and w['end'] <= 3. for w in plan['windows']))
                result = media.render_video([dict(scene, text=segments[0]['text'])], voice, output,
                    dict(width=160, height=240, fps=25, encoder='cpu', preset='fast',
                         duration_seconds=seconds, timeline_plan=plan,
                         transition='fade', transition_seconds=.2, clean_mode='none',
                         subtitle_font_size=20, caption_plate=False,
                         subtitle_segments=[dict(start=0., end=seconds, text=segments[0]['text'])]))
                network.assert_not_called()
            self.assertTrue(output.is_file())
            info = media.probe(output)
            self.assertEqual((info['width'], info['height']), (160, 240))
            self.assertTrue(info['has_audio'])
            self.assertAlmostEqual(info['duration'], seconds, delta=.08)
            media._validate_stream_coverage(info, seconds, 25)
            self.assertEqual(result['transition_seconds'], 0.)
            self.assertEqual(len(result['timeline']), 2)
            self.assertAlmostEqual(result['timeline'][0]['output_start'], 0.)
            self.assertAlmostEqual(result['timeline'][0]['output_end'],
                                   result['timeline'][1]['output_start'])
            self.assertAlmostEqual(result['timeline'][-1]['output_end'], seconds, delta=.04)
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, voice)},
                             original_hashes)
            detection = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'info', '-nostdin',
                '-xerror', '-i', str(output), '-vf',
                'blackdetect=d=0.08:pix_th=0.1:pic_th=0.99,freezedetect=n=-60dB:d=0.2',
                '-an', '-f', 'null', '-'], check=True, capture_output=True, text=True,
                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertNotIn('black_start:', detection.stderr)
            self.assertNotIn('freeze_start:', detection.stderr)


if __name__ == '__main__':
    unittest.main()

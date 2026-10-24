"""Independent renderer checks and real local media for bounded picture slowdown.

All clips and tones are generated locally; these are not paid model results.
"""
import copy
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import media, providers
import test_media as media_fixtures
import test_v2_workflow as workflow_fixtures


def measured_plan(scenes, lengths, speeds):
    windows, rows = [], []
    for i, (scene, playback, speed) in enumerate(zip(scenes, lengths, speeds)):
        sid = f's{i+1}'
        windows.append(dict(clip_id=scene['clip_id'], source_path=scene['source_path'],
            source_sha256=scene['source_sha256'], source_start=scene['start'],
            source_end=scene['end'], start=scene['start'],
            end=scene['start']+playback*speed, unit_index=i, segment_id=sid,
            reuse_index=1, video_speed=speed, playback_seconds=playback))
        rows.append(dict(segment_id=sid, raw_seconds=playback, voice_seconds=playback,
                         video_speed=speed, clip_ids=[scene['clip_id']]))
    return dict(version=1, phase='actual', status='ready', speed=1., repeat_count=0,
                total_seconds=sum(lengths), transition_seconds=0., segments=rows, windows=windows)


class SlowdownValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slowdown-validation-')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source.mp4'
        self.source.write_bytes(b'local-fixture-not-decoded')
        self.scene = dict(clip_id='c', source_path=str(self.source),
            source_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            start=0., end=3., usable=True)
        self.info = dict(duration=3., has_video=True, hdr=False, width=160, height=240)

    def validate(self, plan, units):
        with patch.object(media, 'probe', return_value=self.info):
            return media._validated_timeline([dict(self.scene, text='测试')], units, .2, {}, plan)

    def test_valid_slow_picture_uses_playback_coverage_not_source_span(self):
        plan = measured_plan([self.scene], [3.5], [3/3.5])
        windows, fade = self.validate(plan, [3.5])
        self.assertEqual(fade, 0.)
        self.assertAlmostEqual(windows[0]['end'], 3.)
        self.assertAlmostEqual(windows[0]['playback_seconds'], 3.5)

    def test_default_speed_preserves_legacy_measured_plan(self):
        plan = measured_plan([self.scene], [2.], [1.])
        plan['windows'][0].pop('video_speed')
        plan['windows'][0].pop('playback_seconds')
        windows, _ = self.validate(plan, [2.])
        self.assertEqual(windows[0]['video_speed'], 1.)
        self.assertEqual(windows[0]['playback_seconds'], 2.)

    def test_invalid_or_forged_picture_speed_and_playback_are_rejected(self):
        for speed in (.8499, 1.0001, True, float('nan')):
            plan = measured_plan([self.scene], [2.], [1.])
            plan['windows'][0]['video_speed'] = speed
            with self.subTest(speed=speed), self.assertRaises(media.MediaError):
                self.validate(plan, [2.])
        plan = measured_plan([self.scene], [2.], [1.])
        plan['windows'][0]['playback_seconds'] = 2.25
        with self.assertRaises(media.MediaError):
            self.validate(plan, [2.])

    def test_slowdown_never_authorizes_more_than_two_source_uses(self):
        plan = measured_plan([self.scene], [3.], [1.])
        first = plan['windows'][0]
        plan['windows'] = [dict(first, video_speed=.9, end=.9) for _ in range(3)]
        for w in plan['windows']:
            w['playback_seconds'] = 1.
        plan['repeat_count'] = 2
        with self.assertRaisesRegex(media.MediaError, '复用额度'):
            self.validate(plan, [3.])

    def test_slowdown_cannot_expand_bound_source_range(self):
        plan = measured_plan([self.scene], [3.5], [3/3.5])
        for change in ({'end': 3.1}, {'source_end': 3.5}, {'source_start': .1}):
            changed = copy.deepcopy(plan)
            changed['windows'][0].update(change)
            with self.subTest(change=change), self.assertRaises(media.MediaError):
                self.validate(changed, [3.5])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RealSlowdownMediaTests(unittest.TestCase):
    def test_short_picture_slows_and_long_picture_trims_with_complete_local_export(self):
        with tempfile.TemporaryDirectory(prefix='slowdown-real-') as folder:
            root = Path(folder)
            source, voice, output = root/'moving.mp4', root/'tone.wav', root/'out.mp4'
            workflow_fixtures.ffmpeg('-f', 'lavfi', '-i',
                'testsrc2=size=160x240:rate=25:duration=6', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', source)
            media_fixtures.tone(voice, 5.)
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, voice)}
            scenes = [dict(clip_id=f'c{i}', source_path=str(source), source_sha256=hashes[source],
                           start=float(i*3), end=float((i+1)*3), usable=True) for i in range(2)]
            plan = measured_plan(scenes, [3.5, 1.5], [3/3.5, 1.])
            with patch.object(providers, '_http_open', side_effect=AssertionError('Unexpected network')) as network:
                with patch.object(media, '_run', wraps=media._run) as commands:
                    result = media.render_video([dict(s, text='本地画面适配测试') for s in scenes],
                        voice, output, dict(width=160, height=240, fps=25, encoder='cpu', preset='fast',
                            duration_seconds=5., timeline_plan=plan, transition='none', transition_seconds=0.,
                            clean_mode='none', subtitle_font_size=20, caption_plate=False,
                            min_output_seconds=.1, max_output_seconds=60,
                            subtitle_segments=[dict(start=0., end=3.5, text='短镜头适度慢放'),
                                               dict(start=3.5, end=5., text='长镜头正常裁短')]))
                network.assert_not_called()
            info = media.probe(output)
            self.assertEqual((info['width'], info['height']), (160, 240))
            self.assertTrue(info['has_audio'])
            self.assertAlmostEqual(info['duration'], 5., delta=.08)
            media._validate_stream_coverage(info, 5., 25)
            self.assertEqual(result['transition_seconds'], 0.)
            self.assertEqual(len(result['timeline']), 2)
            short, long = result['timeline']
            self.assertAlmostEqual(short['source_duration'], 3.)
            self.assertAlmostEqual(short['playback_duration'], 3.5)
            self.assertAlmostEqual(short['video_speed'], 3/3.5)
            self.assertAlmostEqual(long['source_duration'], 1.5)
            self.assertAlmostEqual(long['playback_duration'], 1.5)
            self.assertEqual(long['video_speed'], 1.)
            self.assertEqual(long['source_end'], 6.)
            self.assertAlmostEqual(long['end'], 4.5)
            graphs = [c.args[0][c.args[0].index('-filter_complex')+1] for c in commands.call_args_list
                      if '-filter_complex' in c.args[0] and str(source) in list(map(str, c.args[0]))]
            self.assertEqual(len(graphs), 2)
            for graph in graphs:
                self.assertLess(graph.index('trim=duration='), graph.index('setpts=(PTS-STARTPTS)/'))
                self.assertLess(graph.index('setpts=(PTS-STARTPTS)/'), graph.index('fps=25'))
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, voice)}, hashes)
            detection = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'info', '-nostdin', '-xerror',
                '-i', str(output), '-vf',
                'blackdetect=d=0.08:pix_th=0.1:pic_th=0.99,freezedetect=n=-60dB:d=0.2',
                '-an', '-f', 'null', '-'], check=True, capture_output=True, text=True, timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertNotIn('black_start:', detection.stderr)
            self.assertNotIn('freeze_start:', detection.stderr)


if __name__ == '__main__':
    unittest.main()

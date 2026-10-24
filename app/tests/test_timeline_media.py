import copy
import hashlib
from pathlib import Path
import tempfile
import shutil
import subprocess
import wave
import unittest
from unittest.mock import patch

from mixstudio import media
from mixstudio.timeline import plan_timeline


class TimelineMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'source.mp4'
        self.path.write_bytes(b'local-synthetic-video-fixture')
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        scene = dict(clip_id='c1', source_path=str(self.path), source_sha256=self.sha,
                     start=0., end=4., usable=True)
        self.plan = plan_timeline([dict(segment_id='s1', text='a'), dict(segment_id='s2', text='b')],
            [scene], [dict(segment_id='s1', clip_ids=['c1']), dict(segment_id='s2', clip_ids=['c1'])],
            target_seconds=5, transition_seconds=.2, voice_seconds=[3., 3.], phase='actual')
        self.clips = [dict(scene, text='a'), dict(scene, text='b')]
        self.info = dict(duration=4., has_video=True, hdr=False, width=320, height=480)

    def validate(self, plan=None):
        with patch('mixstudio.media.probe', return_value=self.info):
            return media._validated_timeline(self.clips, [3., 3.], .2, {}, plan or self.plan)

    def test_explicit_actual_plan_allows_one_reuse(self):
        windows, fade = self.validate()
        self.assertEqual(len(windows), 2)
        self.assertAlmostEqual(sum(w['end']-w['start'] for w in windows)-fade, 6.)

    def test_estimated_or_over_speed_plan_is_not_renderable(self):
        for change in ({'phase':'estimated'}, {'speed':1.5001}, {'status':'shortage'}, {'total_seconds':7}):
            with self.subTest(change=change), self.assertRaises(media.MediaError):
                self.validate({**self.plan, **change})

    def test_crop_cannot_relabel_original_range_or_source_hash(self):
        for change in ({'source_start':.1}, {'source_end':4.5}, {'source_sha256':'0'*64}, {'end':4.5}):
            plan = copy.deepcopy(self.plan)
            plan['windows'][0].update(change)
            with self.subTest(change=change), self.assertRaises(media.MediaError): self.validate(plan)

    def test_mutated_source_is_rejected(self):
        self.path.write_bytes(b'changed-source')
        with self.assertRaises(media.MediaError): self.validate()

    def test_third_use_rejected_even_if_reported_repeat_zero(self):
        plan = copy.deepcopy(self.plan)
        plan['windows'].append(dict(plan['windows'][-1]))
        plan['repeat_count'] = 0
        with self.assertRaises(media.MediaError): self.validate(plan)

    def test_unallocated_source_and_wrong_unit_rejected(self):
        for change in ({'source_path':str(self.path.parent/'other.mp4')}, {'unit_index':2}):
            plan = copy.deepcopy(self.plan)
            plan['windows'][0].update(change)
            with self.subTest(change=change), self.assertRaises(media.MediaError): self.validate(plan)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class RealTimelineMediaTests(unittest.TestCase):
    def test_measured_repeat_plan_renders_real_video_without_legacy_rejection(self):
        with tempfile.TemporaryDirectory(prefix='timeline-real-') as folder:
            root = Path(folder)
            source, voice, output = root/'source.mp4', root/'voice.wav', root/'out.mp4'
            media._run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-f','lavfi','-i',
                        'testsrc2=size=320x480:rate=25:duration=4','-c:v','libx264','-preset','ultrafast',
                        '-threads','1',str(source)])
            with wave.open(str(voice), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                audio.writeframes(b'\0\0'*24000*6)
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            scene = dict(clip_id='c', source_path=str(source), source_sha256=sha, start=0., end=4., usable=True)
            segments = [dict(segment_id='a',text='第一段'),dict(segment_id='b',text='第二段')]
            plan = plan_timeline(segments,[scene],[dict(segment_id=s['segment_id'],clip_ids=['c']) for s in segments],
                                 target_seconds=5,transition_seconds=.2,voice_seconds=[3.,3.],phase='actual')
            settings = dict(width=320,height=480,fps=25,encoder='cpu',preset='fast',duration_seconds=6,
                            timeline_plan=plan,transition='fade',transition_seconds=.2,
                            subtitle_segments=[dict(start=0.,end=3.,text='第一段'),dict(start=3.,end=6.,text='第二段')])
            result = media.render_video([dict(scene,text=s['text']) for s in segments],voice,output,settings)
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(media.probe(output)['duration'],6.,delta=.08)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),sha)

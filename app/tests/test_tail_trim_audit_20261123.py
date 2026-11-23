import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
import wave

from mixstudio import media
from mixstudio.timeline import plan_timeline


class TailTrimTests(unittest.TestCase):
    def fixture(self, source=Path('product.mp4'), digest='fixture'):
        ranges=[(0.,3.), (6.,6.085), (3.,6.)]
        scenes=[dict(clip_id=str(i), source_path=str(source),source_sha256=digest,
                 start=start,end=end,usable=True) for i,(start,end) in enumerate(ranges)]
        segments=[dict(segment_id=str(i),text='商品画面') for i in (0,1)]
        candidates=[dict(segment_id='0',clip_ids=['0']),dict(segment_id='1',clip_ids=['1','2'])]
        plan=plan_timeline(segments,scenes,candidates,target_seconds=5,transition_seconds=0,
                           phase='actual',max_voice_speed=1.,voice_seconds=[1.001,.85])
        return scenes,plan

    def test_visually_usable_short_window_is_not_trimmed_to_zero_frames(self):
        _,plan=self.fixture()
        self.assertEqual(plan['status'],'ready')
        lengths=[w['playback_seconds'] for w in plan['windows']]
        self.assertAlmostEqual(sum(lengths),1.851)
        for fps in (15,24,25,30,60):
            with self.subTest(fps=fps):
                frames,_=media._frame_timeline(lengths,0,fps,plan['total_seconds'])
                self.assertTrue(all(n>=1 for n in frames))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs FFmpeg')
    def test_ready_plan_with_short_tail_renders_real_mp4(self):
        with tempfile.TemporaryDirectory(prefix='tail-audit-') as folder:
            root=Path(folder);source=root/'s.mp4';voice=root/'voice.wav';out=root/'out.mp4'
            media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                'testsrc2=size=160x160:rate=30:duration=6.1','-threads','1','-c:v','libx264',source])
            sha=hashlib.sha256(source.read_bytes()).hexdigest();scenes,plan=self.fixture(source,sha)
            with wave.open(str(voice),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
                audio.writeframes(b'\0\0'*44424)
            result=media.render_video([{**scenes[0],'text':'看商品'},{**scenes[1],
                'text':'看外观','additional_clips':[scenes[2]]}],voice,out,
                dict(width=160,height=160,fps=30,encoder='cpu',preset='fast',duration_seconds=1.851,
                     transition='none',timeline_plan=plan,visual_unit_seconds=[1.001,.85],
                     subtitle_segments=[dict(start=0.,end=1.001,text='看商品'),dict(start=1.001,end=1.851,text='看外观')]))
            self.assertTrue(result['published'])
            self.assertAlmostEqual(media.probe(out)['duration'],1.851,delta=1/30+.001)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),sha)

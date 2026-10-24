"""Review regressions: stream spans, narrow cleanup, cumulative frame rounding."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import wave
from unittest.mock import patch

from mixstudio import media
from mixstudio.timeline import plan_timeline


def wav(path, seconds):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(48000)
        out.writeframes(b'\0\0' * round(seconds * 48000))


class ReviewMediaUnitTests(unittest.TestCase):
    def test_frame_rounding_respects_hard_output_cap(self):
        lengths, fade = media._frame_timeline([30.0005, 29.9995], 0, 30, 60, 60)
        self.assertEqual(sum(lengths), 1800)
        lengths, _ = media._frame_timeline([30.01], 0, 30, 30.01, 30.01)
        self.assertLessEqual(sum(lengths)/30, 30.01)
        self.assertLess(30.01-sum(lengths)/30, 1/30)

    def test_fractional_fade_and_endpoints_share_one_frame_grid(self):
        raw = [1.013] * 20
        fade = .17
        duration = sum(raw)-fade*19
        frames, actual_fade = media._frame_timeline(raw, fade, 30, duration)
        self.assertLessEqual(actual_fade, fade)
        self.assertLessEqual(abs((sum(frames)/30-actual_fade*19)-duration), 1/30)
        self.assertTrue(all(n/30-r <= 1/30 for n,r in zip(frames,raw)))

    def test_frame_allocation_uses_cumulative_endpoints(self):
        lengths, fade = media._frame_timeline([1.013] * 20, 0, 30, 20.26)
        self.assertEqual(sum(lengths), 608)
        self.assertEqual(fade, 0)
        for index in range(1, 20):
            self.assertLessEqual(abs(sum(lengths[:index]) / 30 - 1.013 * index), 1/30)
        self.assertTrue(all(abs(n/30 - 1.013) <= 1/30 for n in lengths))

    def test_short_video_track_is_rejected_independently_of_container(self):
        info = dict(duration=20.26, video_duration=20., video_start_time=0., video_end_time=20.,
                    audio_duration=20.26, audio_start_time=0., audio_end_time=20.26, format_start_time=0.)
        with self.assertRaisesRegex(media.MediaError, '视频轨'):
            media._validate_stream_coverage(info, 20.26, 30)

    def test_source_window_cannot_enter_audio_only_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.mp4'; path.write_bytes(b'fixture')
            info = dict(duration=5, width=320, height=240, hdr=False, video_duration=2,
                        video_start_time=10, video_end_time=12, format_start_time=10)
            with patch.object(media, 'probe', return_value=info):
                with self.assertRaises(media.MediaError):
                    media._window(dict(source_path=str(path), start=2.2, end=3), {})
                window = media._window(dict(source_path=str(path), start=0, end=2), {})
                self.assertEqual(window['end'], 2)

    def test_audio_span_shortage_is_rejected(self):
        info = dict(duration=20, video_duration=20., video_start_time=0., video_end_time=20.,
                    audio_duration=18., audio_start_time=0., audio_end_time=18., format_start_time=0.)
        with self.assertRaisesRegex(media.MediaError, '音轨'):
            media._validate_stream_coverage(info, 20., 30)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class ReviewMediaRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='mix-review-media-fix-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root/'source.mp4'
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'testsrc2=size=320x240:rate=30:duration=64','-c:v','libx264',
                    '-preset','ultrafast','-threads','1',cls.source])
        cls.sha = hashlib.sha256(cls.source.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_narrow_blur_band_renders_on_small_source(self):
        voice = self.root/'blur.wav'; wav(voice, 1)
        result = media.render_video([dict(source_path=str(self.source),start=0,end=2,text='商品')],
            voice, self.root/'blur.mp4',dict(width=320,height=480,fps=30,encoder='cpu',preset='fast',
                clean_mode='blur',clean_height_pct=3,clean_top_pct=75,transition='none',
                subtitle_font_size=20,subtitle_segments=[dict(start=0,end=1,text='商品')]))
        self.assertAlmostEqual(result['duration'], 1, delta=1/30)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha)

    def test_hard_cuts_do_not_accumulate_video_duration_loss(self):
        n, unit = 20, 1.013
        scenes = [dict(clip_id=f'c{i}',start=3*i,end=3*i+3,source_path=str(self.source),
                       source_sha256=self.sha,usable=True) for i in range(n)]
        segments = [dict(segment_id=f's{i}',text='商品展示') for i in range(n)]
        choices = [dict(segment_id=f's{i}',clip_ids=[f'c{i}']) for i in range(n)]
        plan = plan_timeline(segments,scenes,choices,target_seconds=n*unit,transition_seconds=0,
                             voice_seconds=[unit]*n,phase='actual')
        voice = self.root/'hard.wav'; wav(voice,n*unit)
        output = self.root/'hard.mp4'
        result = media.render_video([dict(s,text='商品展示') for s in scenes],voice,output,
            dict(width=160,height=160,fps=30,encoder='cpu',preset='fast',caption_plate=False,
                 subtitle_font_size=12,transition='none',transition_seconds=0,timeline_plan=plan,
                 duration_seconds=n*unit,subtitle_segments=[dict(start=i*unit,end=(i+1)*unit,text='商品展示') for i in range(n)]))
        streams=json.loads(media._run(['ffprobe','-v','error','-show_streams','-of','json',output]))['streams']
        video=next(s for s in streams if s['codec_type']=='video')
        audio=next(s for s in streams if s['codec_type']=='audio')
        self.assertLessEqual(abs(float(video['duration'])-float(audio['duration'])),1/30+.001)
        self.assertLessEqual(abs(result['timeline'][-1]['output_end']-n*unit),1/30+.001)
        for i,row in enumerate(result['timeline']):
            self.assertLessEqual(abs(row['output_end']-(i+1)*unit),1/30+.001)

    def test_video_duration_excludes_longer_audio(self):
        path=self.root/'audio-tail.mp4'
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'color=size=160x160:rate=30:duration=2','-f','lavfi','-i',
                    'sine=frequency=440:duration=5','-c:v','libx264','-preset','ultrafast',
                    '-threads','1','-c:a','aac',path])
        info=media.probe(path)
        self.assertAlmostEqual(info['duration'],5,delta=.1)
        self.assertAlmostEqual(info['video_duration'],2,delta=.04)
        self.assertAlmostEqual(info['audio_duration'],5,delta=.04)

    def test_nonzero_origin_retains_absolute_stream_bounds(self):
        path=self.root/'offset.mp4'
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'color=size=160x160:rate=30:duration=2','-c:v','libx264',
                    '-preset','ultrafast','-threads','1','-output_ts_offset','10',path])
        info=media.probe(path)
        self.assertAlmostEqual(info['video_start_time'],10,delta=.04)
        self.assertAlmostEqual(info['video_end_time'],12,delta=.04)
        self.assertAlmostEqual(info['format_start_time'],10,delta=.04)
        self.assertAlmostEqual(info['video_duration'],2,delta=.04)

    def test_missing_stream_duration_uses_packet_span_not_audio_tail(self):
        path=self.root/'packet-tail.mkv'
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'color=size=160x160:rate=25:duration=2','-f','lavfi','-i',
                    'sine=frequency=440:duration=5','-c:v','libx264','-preset','ultrafast',
                    '-threads','1','-c:a','pcm_s16le',path])
        info=media.probe(path)
        self.assertAlmostEqual(info['duration'],5,delta=.1)
        self.assertAlmostEqual(info['video_duration'],2,delta=.06)
        self.assertAlmostEqual(info['audio_duration'],5,delta=.06)

    def test_vfr_packet_span_does_not_use_average_frame_count(self):
        path=self.root/'vfr-tail.mkv'
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'testsrc2=size=160x160:rate=25:duration=2','-f','lavfi','-i',
                    'sine=frequency=440:duration=5','-vf',"select='not(mod(n,3))+not(mod(n,7))'",
                    '-fps_mode','vfr','-c:v','libx264','-preset','ultrafast',
                    '-threads','1','-c:a','pcm_s16le',path])
        info=media.probe(path)
        packets=json.loads(media._run(['ffprobe','-v','error','-select_streams','v:0',
                    '-show_packets','-show_entries','packet=pts_time,duration_time','-of','json',path]))['packets']
        pts=[float(p['pts_time']) for p in packets]
        self.assertGreater(len({round(b-a,3) for a,b in zip(pts,pts[1:])}),1)
        end=max(float(p['pts_time'])+float(p['duration_time']) for p in packets)
        self.assertAlmostEqual(info['video_end_time'],end,places=6)
        self.assertLess(info['video_duration'],info['duration']-2)

if __name__=='__main__': unittest.main()

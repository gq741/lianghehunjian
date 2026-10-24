"""B03/B07/B08 regressions: isolated local publication and real FFmpeg only."""
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

from mixstudio import media
from mixstudio.jobs import Cancelled


class PublishReview2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'valid-staged.mp4';self.source.write_bytes(b'valid-staged-video-fixture')
        self.dest=self.root/'final.mp4'
        self.no_link=patch.object(media.os,'link',side_effect=OSError(errno.ENOTSUP,'fixture unsupported link'))

    def assert_clean(self):
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['valid-staged.mp4'] if not self.dest.exists() else ['final.mp4','valid-staged.mp4'])
        self.assertEqual(self.source.read_bytes(),b'valid-staged-video-fixture')

    def test_copy_interruption_leaves_no_final_file_and_retry_succeeds(self):
        def fail_copy(incoming, output):
            output.write(incoming.read(7))
            raise OSError(errno.ENOSPC,'fixture disk full')
        with self.no_link, patch.object(media.shutil,'copyfileobj',side_effect=fail_copy):
            with self.assertRaises(OSError):media._publish(self.source,self.dest)
        self.assertFalse(self.dest.exists());self.assert_clean()
        with self.no_link:media._publish(self.source,self.dest)
        self.assertEqual(self.dest.read_bytes(),self.source.read_bytes());self.assert_clean()

    def test_hardlink_success_preserves_source_and_target(self):
        media._publish(self.source,self.dest)
        self.assertEqual(self.dest.read_bytes(),self.source.read_bytes());self.assert_clean()

    def test_existing_target_always_wins(self):
        self.dest.write_bytes(b'user-existing')
        with self.assertRaises(media.MediaError):media._publish(self.source,self.dest)
        self.assertEqual(self.dest.read_bytes(),b'user-existing');self.assert_clean()

    @unittest.skipUnless(os.name=='nt','Windows no-replace rename contract')
    def test_concurrent_target_created_before_rename_is_not_overwritten(self):
        rename=os.rename
        def raced_rename(source,destination):
            Path(destination).write_bytes(b'concurrent-user-result')
            return rename(source,destination)
        with self.no_link,patch.object(media.os,'rename',side_effect=raced_rename):
            with self.assertRaises(media.MediaError):media._publish(self.source,self.dest)
        self.assertEqual(self.dest.read_bytes(),b'concurrent-user-result');self.assert_clean()

    @unittest.skipUnless(os.name=='nt','Windows no-replace rename contract')
    def test_atomic_commit_error_removes_only_this_temp(self):
        with self.no_link,patch.object(media.os,'rename',side_effect=OSError(errno.EACCES,'fixture denied')):
            with self.assertRaises(OSError):media._publish(self.source,self.dest)
        self.assertFalse(self.dest.exists());self.assert_clean()

    def test_source_open_failure_never_creates_final_file(self):
        missing=self.root/'missing.mp4'
        with self.no_link:
            with self.assertRaises(OSError):media._publish(missing,self.dest)
        self.assertFalse(self.dest.exists());self.assert_clean()

    @unittest.skipUnless(os.name=='nt','Windows no-replace rename contract')
    def test_two_fallback_publishers_have_exactly_one_intact_winner(self):
        second=self.root/'second.mp4';second.write_bytes(b'second-valid-source'*100)
        def publish(path):
            try:media._publish(path,self.dest);return 'published'
            except media.MediaError:return 'exists'
        with self.no_link,ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(publish,[self.source,second]))
        self.assertEqual(sorted(results),['exists','published'])
        self.assertIn(self.dest.read_bytes(),[self.source.read_bytes(),second.read_bytes()])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['final.mp4','second.mp4','valid-staged.mp4'])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class RealMediaReview2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='review2-media-');cls.root=Path(cls.tmp.name)
        cls.sources={}
        for sar in (1,2):
            base=cls.root/f'base-sar{sar}.mp4'
            cls.ffmpeg('-f','lavfi','-i','color=c=red:s=320x160:r=30:d=2','-vf',f'setsar={sar}',
                       '-c:v','libx264','-threads','1','-pix_fmt','yuv420p',base)
            cls.sources[(sar,0)]=base
            for rotation in (90,180,270):
                target=cls.root/f'rotate-sar{sar}-{rotation}.mp4'
                cls.ffmpeg('-display_rotation',str(rotation),'-i',base,'-c','copy',target)
                cls.sources[(sar,rotation)]=target
        cls.voice=cls.root/'voice.wav'
        with wave.open(str(cls.voice),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(48000);wav.writeframes(b'\0\0'*48000)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    @staticmethod
    def ffmpeg(*args):
        result=subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n',*map(str,args)],
            capture_output=True,timeout=40,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:raise AssertionError(result.stderr.decode('utf8','replace'))
        return result

    def render(self,source,output,**kwargs):
        return media.render_video([{'source_path':str(source),'start':0,'end':2,'text':''}],
            self.voice,output,{'width':320,'height':320,'fps':30,'transition':'none','transition_seconds':0,
            'caption_plate':False,'encoder':'cpu','preset':'fast','subtitle_font_size':12},**kwargs)

    def red_box(self,path):
        data=self.ffmpeg('-ss','0.5','-i',path,'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1').stdout
        self.assertEqual(len(data),320*320*3)
        points=[(i%320,i//320) for i in range(320*320) if data[i*3]>180 and data[i*3+1]<60 and data[i*3+2]<60]
        return max(x for x,y in points)-min(x for x,y in points)+1,max(y for x,y in points)-min(y for x,y in points)+1

    def test_rotated_non_square_sar_has_correct_measured_geometry(self):
        for rotation in (90,270):
            with self.subTest(rotation=rotation):
                source=self.sources[2,rotation];before=hashlib.sha256(source.read_bytes()).hexdigest()
                info=media.probe(source)
                self.assertEqual((info['width'],info['height']),(160,320))
                self.assertAlmostEqual(info['sample_aspect_ratio'],.5)
                output=self.root/f'fixed-{rotation}.mp4';self.render(source,output)
                self.assertEqual(self.red_box(output),(80,320))
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

    def test_unrotated_and_square_sar_geometry_remains_correct(self):
        for sar,rotation,expected in ((2,0,(320,80)),(2,180,(320,80)),(1,0,(320,160)),(1,90,(160,320)),(1,270,(160,320))):
            with self.subTest(sar=sar,rotation=rotation):
                source=self.sources[sar,rotation];before=source.read_bytes()
                output=self.root/f'control-{sar}-{rotation}.mp4';self.render(source,output)
                self.assertEqual(self.red_box(output),expected);self.assertEqual(source.read_bytes(),before)

    def test_late_cancel_after_publish_returns_committed_result(self):
        output=self.root/'late-cancel.mp4';event=threading.Event();published=media._publish
        progress_calls_after_publish=[]
        def publish_then_cancel(source,destination):
            published(source,destination);event.set()
        def progress(value,message):
            if event.is_set():
                progress_calls_after_publish.append((value,message))
                raise Cancelled('late cancel fixture')
        with patch.object(media,'_publish',side_effect=publish_then_cancel):
            result=self.render(self.sources[1,0],output,cancel=event,progress=progress)
        self.assertTrue(output.is_file());self.assertGreater(output.stat().st_size,0)
        self.assertEqual(result['path'],str(output));self.assertIs(result.get('published'),True)
        self.assertEqual(progress_calls_after_publish,[])

    def test_prepublication_cancel_still_raises_and_produces_no_output(self):
        output=self.root/'early-cancel.mp4';event=threading.Event();event.set()
        with self.assertRaises(media.MediaCancelled):self.render(self.sources[1,0],output,cancel=event)
        self.assertFalse(output.exists())

    def test_cancel_at_final_progress_is_checked_before_commit(self):
        output=self.root/'final-progress-cancel.mp4';event=threading.Event()
        def progress(value,message):
            if value>=1:event.set()
        with self.assertRaises(media.MediaCancelled):
            self.render(self.sources[1,0],output,cancel=event,progress=progress)
        self.assertFalse(output.exists())

    def test_progress_error_before_publish_is_not_swallowed(self):
        output=self.root/'progress-error.mp4'
        def progress(value,message):raise RuntimeError('progress fixture error')
        with self.assertRaisesRegex(RuntimeError,'progress fixture error'):
            self.render(self.sources[1,0],output,progress=progress)
        self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()

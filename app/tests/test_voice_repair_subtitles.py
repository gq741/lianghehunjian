"""Offline regressions: OCR/cache recovery, bounded repair, HDR and cancellation."""
import json
import io
import contextlib
import shutil
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio import media, preprocess, providers, storage, subtitle_regions
from mixstudio.media_identity import MediaIdentityCache
from mixstudio.store import Store


def box(y=380, x=100, right=540):
    return [[[x,y],[right,y],[right,y+40],[x,y+40]],'原视频字幕测试',.99]


class GeometryRepairTests(unittest.TestCase):
    def test_single_outlier_in_three_frames_does_not_erase(self):
        self.assertIsNone(subtitle_regions.caption_band([[box()],[],[]],640,480))

    def test_consistent_boxes_retain_horizontal_geometry_and_ignore_outlier(self):
        band=subtitle_regions.caption_band([[box()],[box(382)],[box(260)]],640,480)
        self.assertIn('left_pct',band)
        self.assertGreater(band['left_pct'],10)
        self.assertLess(band['width_pct'],80)
        self.assertGreater(band['top_pct'],70)

    def test_malformed_worker_rows_are_ignored(self):
        self.assertIsNone(subtitle_regions.caption_band([None,{},['invalid']],640,480))

    def test_two_supported_caption_lines_are_repaired_together(self):
        band=subtitle_regions.caption_band([[box(330),box(380)],[box(332),box(382)],[]],640,480)
        self.assertLess(band['top_pct'],69)
        self.assertGreater(band['height_pct'],18)

    def test_ocr_probe_fingerprint_changes_without_loading_model(self):
        with patch('importlib.metadata.version',side_effect=['1','2']), \
             patch('importlib.util.find_spec',return_value=None):
            first=subtitle_regions.probe_fingerprint();second=subtitle_regions.probe_fingerprint()
        self.assertNotEqual(first,second)


class SubtitleCacheRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'work').mkdir()
        self.scene={'clip_id':'c','source_sha256':'a'*64,'source_path':str(self.root/'source.mp4')}
        self.plan={'windows':[{'clip_id':'c','start':0.,'end':3.}]}
        self.results=[[box()],[box(382)],[]]
        self.calls=0

    def frame(self,args,**kwargs):
        Path(args[-1]).write_bytes(b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',640,480))

    def worker(self,args,**kwargs):
        self.calls+=1
        output=Path(args[args.index('--output')+1])
        output.write_text(json.dumps(self.results),encoding='utf8')
        return 0,''

    def locate(self,**kwargs):
        with patch.object(media,'probe',return_value={'width':640,'height':480}), \
             patch.object(media,'_run',side_effect=self.frame), \
             patch.object(providers,'_run_process',side_effect=self.worker):
            return subtitle_regions.locate(SimpleNamespace(root=self.root),self.plan,[self.scene],None,lambda *a:None,**kwargs)

    def cache(self): return next((self.root/'subtitle-cache').glob('*.json'))

    def test_corrupt_cache_is_recomputed_not_fatal(self):
        self.locate()
        for text in ('{broken','[]','{"band":{"top_pct":99,"height_pct":30}}'):
            before=self.calls;self.cache().write_text(text,'utf8');self.locate()
            self.assertEqual(self.calls,before+1)
            self.assertIsNotNone(self.plan['windows'][0]['cleanup_band'])

    def test_recent_negative_cache_reuses_but_expires(self):
        self.results=[[],[],[]]
        with patch('time.time',return_value=10000):
            warnings=self.locate();self.locate()
        self.assertTrue(any('短暂' in w and '漏检' in w for w in warnings))
        self.assertTrue(any('未盲目抹除' in w for w in warnings))
        self.assertEqual(self.calls,1)
        self.results=[[box()],[box()],[]]
        with patch('time.time',return_value=10000+86400):self.locate()
        self.assertEqual(self.calls,2)
        self.assertIsNotNone(self.plan['windows'][0]['cleanup_band'])

    def test_explicit_refresh_rechecks_negative_cache(self):
        self.results=[[],[],[]];self.locate();self.results=[[box()],[box()],[]]
        self.locate(refresh=True)
        self.assertEqual(self.calls,2)

    def test_worker_failure_is_classified_and_never_silently_skipped(self):
        self.results={'error':{'code':'missing_dependency'}}
        with self.assertRaisesRegex(ValueError,'依赖|missing_dependency'):
            self.locate()
        self.assertFalse(list((self.root/'subtitle-cache').glob('*.json')))

    def test_timeout_reports_local_retry_without_unknown_remote_result(self):
        with patch.object(media,'probe',return_value={}),patch.object(media,'_run',side_effect=self.frame), \
             patch.object(providers,'_run_process',side_effect=providers.ProviderError('timeout',code='timeout')):
            with self.assertRaisesRegex(ValueError,'本地|重试'):
                subtitle_regions.locate(SimpleNamespace(root=self.root),self.plan,[self.scene],None,lambda *a:None)

    def test_cancel_is_not_changed_to_ocr_failure(self):
        with patch.object(media,'probe',return_value={}),patch.object(media,'_run',side_effect=self.frame), \
             patch.object(providers,'_run_process',side_effect=providers.ProviderError('cancelled',code='cancelled')):
            with self.assertRaises(providers.ProviderError) as caught:
                subtitle_regions.locate(SimpleNamespace(root=self.root),self.plan,[self.scene],None,lambda *a:None)
        self.assertEqual(caught.exception.code,'cancelled')

    def test_broken_png_is_classified(self):
        def bad_frame(args,**kw):Path(args[-1]).write_bytes(b'broken')
        with patch.object(media,'probe',return_value={}),patch.object(media,'_run',side_effect=bad_frame), \
             patch.object(providers,'_run_process',side_effect=self.worker):
            with self.assertRaisesRegex(ValueError,'frame_invalid'):
                subtitle_regions.locate(SimpleNamespace(root=self.root),self.plan,[self.scene],None,lambda *a:None)

    def test_worker_distinguishes_missing_dependency_and_model_loading(self):
        from mixstudio import subtitle_worker
        import sys
        cases=[(None,'missing_dependency'),
               (SimpleNamespace(RapidOCR=lambda **kw:(_ for _ in ()).throw(ValueError('model broken'))),'model_load'),
               (SimpleNamespace(RapidOCR=lambda **kw:(_ for _ in ()).throw(MemoryError())),'out_of_memory')]
        for index,(module,expected) in enumerate(cases):
            output=self.root/f'error-{index}.json'
            with patch.object(sys,'argv',['subtitle_worker','--input','unused','--output',str(output)]), \
                 patch.dict(sys.modules,{'rapidocr_onnxruntime':module}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(subtitle_worker.main(),1)
            self.assertEqual(json.loads(output.read_text('utf8'))['error']['code'],expected)


class HdrCancellationRepairTests(unittest.TestCase):
    def test_prepare_does_not_trust_stale_hdr_metadata(self):
        with patch.object(media,'_path',return_value=Path('fixture.mp4')), \
             patch.object(preprocess,'digest',return_value='a'*64), \
             patch.object(preprocess,'file_version',return_value=(1,2,3,4,5)), \
             patch.object(media,'probe',return_value={'hdr':True,'dolby_vision_profile':5}) as probe:
            with self.assertRaisesRegex(media.MediaError,'Profile 5'):
                preprocess.prepare({'path':'fixture.mp4','sha256':'a'*64},'unused',source_info={'hdr':False})
        self.assertEqual(probe.call_count,1)

    def test_original_descriptor_has_probe_version_and_source_version(self):
        with patch.object(media,'_path',return_value=Path('fixture.mp4')), \
             patch.object(preprocess,'digest',return_value='a'*64), \
             patch.object(preprocess,'file_version',return_value=(1,2,3,4,5)), \
             patch.object(media,'probe',return_value={'hdr':False}):
            record=preprocess.prepare({'path':'fixture.mp4','sha256':'a'*64},'unused')
        self.assertGreaterEqual(record.get('probe_version',0),2)
        self.assertEqual(record.get('source_version'),[1,2,3,4,5])

    def test_render_sdr_validation_receives_cancel_and_local_identity_cache(self):
        event=threading.Event();cache=MediaIdentityCache()
        with patch.object(media,'_path',return_value=Path('fixture.mp4')), \
             patch.object(media,'probe',return_value={'hdr':True}), \
             patch.object(preprocess,'validated',side_effect=RuntimeError('capture')) as validate:
            with self.assertRaisesRegex(RuntimeError,'capture'):
                media._window({'source_path':'fixture.mp4','prepared_media':{'mode':'sdr'}},{},cancel=event,identity_cache=cache)
        self.assertIs(validate.call_args.kwargs['cancel'],event)
        self.assertIs(validate.call_args.kwargs['identity_cache'],cache)

    def test_source_hash_loop_observes_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.mp4';source.write_bytes(b'x'*1024)
            event=threading.Event();event.set()
            plan={'version':1,'phase':'actual','status':'ready','speed':1.,'transition_seconds':0.,
                  'total_seconds':.2,'segments':[{}],'windows':[{}]}
            with patch.object(media,'_window',return_value={'source_path':source,'start':0.,'end':1.}):
                with self.assertRaises(media.MediaCancelled):
                    media._validated_timeline([{'source_path':str(source)}],[.2],0,{},plan,cancel=event)


class SubtitleStorageRepairTests(unittest.TestCase):
    def test_subtitle_cache_in_inventory_and_recoverable_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite');cache=root/'subtitle-cache';cache.mkdir()
            path=cache/('a'*64+'.json');path.write_text(json.dumps({'source_sha256':'b'*64,'band':None}),'utf8')
            data=storage.inventory(root,store=store)
            self.assertIn('subtitle-cache',[c['id'] for c in data['categories']])
            with patch.object(storage,'recycle_file') as move:
                storage.cleanup(root,store=store,confirmed=True,include_caches=True)
            move.assert_called_once_with(path)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class BoundedRepairRenderTests(unittest.TestCase):
    def test_delogo_and_blur_use_horizontal_ocr_region_in_real_export(self):
        from mixstudio.timeline import plan_timeline
        import hashlib,wave
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.mp4';voice=root/'voice.wav'
            media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                'testsrc2=size=320x240:rate=25:duration=1','-c:v','libx264','-preset','ultrafast','-threads','1',source])
            with wave.open(str(voice),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000);out.writeframes(b'\0\0'*28800)
            sha=hashlib.sha256(source.read_bytes()).hexdigest()
            clip={'clip_id':'c','source_path':str(source),'source_sha256':sha,'start':0.,'end':1.,'usable':True,'text':'商品'}
            for mode in ('delogo','blur'):
                plan=plan_timeline([{'segment_id':'s','text':'商品'}],[clip],
                    [{'segment_id':'s','clip_ids':['c']}],target_seconds=5,transition_seconds=0,voice_seconds=[.6],phase='actual')
                plan['windows'][0]['cleanup_band']={'top_pct':70.,'height_pct':12.,'left_pct':20.,'width_pct':40.}
                with patch.object(media,'_run',wraps=media._run) as run:
                    result=media.render_video([clip],voice,root/(mode+'.mp4'),{'width':320,'height':480,
                        'fps':25,'encoder':'cpu','preset':'fast','transition':'none','clean_mode':mode,
                        'subtitle_font_size':20,'timeline_plan':plan,'subtitle_segments':[{'start':0.,'end':.6,'text':'商品'}]})
                graphs=[str(c.args[0][c.args[0].index('-filter_complex')+1]) for c in run.call_args_list if '-filter_complex' in c.args[0]]
                self.assertTrue(any(('delogo=x=64:' in g and ':w=128:' in g) if mode=='delogo' else 'crop=128:29:64:168' in g for g in graphs),graphs)
                self.assertAlmostEqual(result['duration'],.6,delta=.05)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),sha)

class SubtitleStorageReferenceTests(unittest.TestCase):
    def test_referenced_subtitle_cache_and_original_survive_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite');cache=root/'subtitle-cache';cache.mkdir()
            path=cache/('a'*64+'.json');path.write_text(json.dumps({'source_sha256':'b'*64,'band':None}),'utf8')
            store.put_asset({'id':'source','path':str(root/'original.mp4'),'sha256':'b'*64})
            store.put_draft({'id':'draft','asset_ids':['source'],'created_at':1})
            with patch.object(storage,'recycle_file') as move:
                storage.cleanup(root,store=store,confirmed=True,include_caches=True)
            move.assert_not_called()

    def test_subtitle_locator_temporary_prefix_is_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite');folder=root/'work'/'subtitle-locate-fixture';folder.mkdir(parents=True)
            path=folder/'images.json';path.write_text('[]','utf8')
            import os
            old=time.time()-9*86400;os.utime(path,(old,old))
            with patch.object(storage,'recycle_file') as move:
                storage.cleanup(root,store=store,confirmed=True)
            move.assert_called_once_with(path)


if __name__=='__main__':unittest.main()

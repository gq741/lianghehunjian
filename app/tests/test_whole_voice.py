"""New production route; provider calls mocked, actual FFmpeg/PCM verification."""
from pathlib import Path
from datetime import datetime
import json
import io
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from mixstudio.service import Studio, fingerprint
from mixstudio.whole_voice import _pcm_duration, _hash, _key, _atomic
from mixstudio.voice_timing import align_segments
from mixstudio import media, providers, script_engine, vision


TEXT = '看看这款商品的外观。再了解它的细节。'
SEGMENTS = [{'segment_id':'s1','text':'看看这款商品的外观。'},
            {'segment_id':'s2','text':'再了解它的细节。'}]


def wav(path, seconds=5.):
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(44100)
        out.writeframes(b'\0\0'*round(seconds*44100))


class TimingTests(unittest.TestCase):
    def words(self):
        return [{'word':SEGMENTS[0]['text'],'start':.1,'end':2.5},
                {'word':SEGMENTS[1]['text'],'start':2.7,'end':4.8}]

    def test_boundaries_use_measured_onsets_not_character_rate(self):
        result=align_segments(SEGMENTS,self.words(),5.)
        self.assertEqual(result['segment_seconds'],[2.7,2.3])
        self.assertEqual(result['captions'][1]['start'],2.7)
        self.assertEqual(''.join(c['text'] for c in result['captions']),TEXT)

    def test_unrelated_transcript_stops_before_vision(self):
        with self.assertRaises(ValueError):
            align_segments(SEGMENTS,[{'word':'完全不同','start':0.,'end':1.}],5.)

    def test_invalid_times_and_punctuation_only_are_rejected(self):
        for start,end in [(float('nan'),1.),(-1,1),(1,7),(2,1)]:
            with self.assertRaises(ValueError):
                align_segments(SEGMENTS,[{'word':TEXT,'start':start,'end':end}],5.)
        with self.assertRaises(ValueError):
            align_segments([{'segment_id':'x','text':'！！'}],self.words(),5.)


class TransportCommitTests(unittest.TestCase):
    def test_complete_framed_audio_commits_despite_cancel_on_last_read(self):
        event=threading.Event()
        class Response:
            status=200
            def __init__(self,data):
                self.body=io.BytesIO(data)
                self.headers={'Content-Type':'audio/wav','Content-Length':str(len(data))}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):
                value=self.body.read(n)
                if self.body.tell()==len(self.body.getvalue()):event.set()
                return value
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wav(root/'raw.wav',1.)
            with patch.object(providers,'_http_open',return_value=Response((root/'raw.wav').read_bytes())):
                providers.synthesize_voice('测试配音',root/'saved.wav',api_key='offline-placeholder',
                    cancel=event,commit_complete=True)
            self.assertEqual(_pcm_duration(root/'saved.wav'),1.)

    def test_partial_response_cancel_retains_unknown_not_a_success(self):
        event=threading.Event()
        class Response:
            status=200;headers={'Content-Type':'audio/wav','Content-Length':'10000'}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):event.set();return b'partial'
        with patch.object(providers,'_http_open',return_value=Response()),self.assertRaises(providers.ProviderError) as caught:
            providers._post(providers.FISH_URL,{},'offline-placeholder',cancel=event,
                            require_framing=True,commit_complete=True)
        self.assertTrue(caught.exception.result_unknown)


class SubtitleRegionTests(unittest.TestCase):
    def test_lower_caption_band_but_not_upper_product_label(self):
        from mixstudio.subtitle_regions import caption_band
        lower=[[[100,380],[540,380],[540,420],[100,420]],'原视频字幕测试',.99]
        upper=[[[100,50],[540,50],[540,90],[100,90]],'商品参数文字',.99]
        band=caption_band([[upper,lower]],640,480)
        self.assertGreater(band['top_pct'],70)
        self.assertLess(band['height_pct'],20)
        self.assertIsNone(caption_band([[upper]],640,480))

    def test_no_detection_does_not_guess_or_erase_fixed_band(self):
        from mixstudio.subtitle_regions import caption_band
        self.assertIsNone(caption_band([[]],640,480))

    def test_small_and_low_confidence_labels_are_ignored(self):
        from mixstudio.subtitle_regions import caption_band
        box=[[100,380],[540,380],[540,420],[100,420]]
        self.assertIsNone(caption_band([[[box,'小米',.99],[box,'字幕内容',.2]]],640,480))


class ProductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=tempfile.TemporaryDirectory()
        cls.video=Path(cls.fixtures.name)/'product.mp4'
        media._run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-f','lavfi','-i',
                    'testsrc2=s=320x240:r=30:d=7','-c:v','libx264','-threads','2','-pix_fmt','yuv420p',cls.video])

    @classmethod
    def tearDownClass(cls): cls.fixtures.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.studio=Studio(self.root/'runtime')
        model=self.root/'model';model.mkdir()
        for name in ('model.bin','config.json','tokenizer.json'):(model/name).write_bytes(b'fixture')
        self.studio.settings.update({'output_dir':str(self.root/'out'),'width':320,'height':240,
            'subtitle_font_size':20,'asr_model_path':str(model),'clean_mode':'delogo','transition':'none'})
        self.asset={'id':'a','name':'product.mp4','path':str(self.video),'sha256':fingerprint(self.video),
            'duration':7.,'width':320,'height':240,'media_info':media.probe(self.video),
            'clips':[],'libraries':['local'],'imported_at':1}
        self.studio.store.put_asset(self.asset)
        self.draft={'id':'d','workflow':'script_first_v2','production_mode':'whole_voice_v1',
            'voiceover':TEXT,'segments':SEGMENTS,'title':'测试','product_name':'商品','asset_ids':['a'],
            'target_seconds':25,'revision':1,'created_at':1,'clips':[],'matches':[],
            'match_status':'pending','status':'draft'}
        self.studio.store.put_draft(self.draft)
        self.secrets=patch.object(self.studio.settings,'secret',return_value='offline-placeholder');self.secrets.start()
        self.network=patch('mixstudio.providers._post',side_effect=AssertionError('live paid calls forbidden'));self.network.start()
        self.calls=[]

    def tearDown(self):
        self.studio.close();self.network.stop();self.secrets.stop();self.temp.cleanup()

    def wait(self,job):
        until=time.monotonic()+30
        while time.monotonic()<until:
            value=self.studio.store.job(job['id'])
            if value['status'] in {'completed','failed','cancelled'}:return value
            time.sleep(.02)
        self.fail('local task timed out')

    def voice(self,text,path,**kwargs):
        self.calls.append(('fish',text));wav(path)
        self.assertTrue(kwargs['commit_complete'])
        return {'duration':5.}

    def timing(self,draft,audio,signature,duration,settings,event):
        self.calls.append(('alignment',duration))
        return align_segments(draft['segments'],[
            {'word':SEGMENTS[0]['text'],'start':.1,'end':2.5},
            {'word':SEGMENTS[1]['text'],'start':2.7,'end':4.8}],duration)

    def scenes(self,*args,**kwargs):
        self.calls.append(('glm',1))
        return {'scenes':[{'clip_id':'c','asset_id':'a','source_path':str(self.video),
                'start':0.,'end':6.,'text':'商品外观','tags':['外观'],'usable':True,
                'confidence':.99,'reason':'外观展示'}],'warnings':[]}

    def matching(self,*args,**kwargs):
        self.calls.append(('deepseek',1))
        return {'candidates':[{'segment_id':s['segment_id'],'clip_ids':['c']} for s in SEGMENTS]}

    def patches(self):
        from contextlib import ExitStack
        stack=ExitStack()
        stack.enter_context(patch.object(self.studio,'_free_preflight',side_effect=lambda *a,**k:self.calls.append(('preflight',0))))
        stack.enter_context(patch.object(providers,'synthesize_voice',side_effect=self.voice))
        stack.enter_context(patch.object(self.studio,'_align_whole',side_effect=self.timing))
        stack.enter_context(patch.object(vision,'analyze_asset',side_effect=self.scenes))
        stack.enter_context(patch.object(script_engine,'match_segments',side_effect=self.matching))
        stack.enter_context(patch('mixstudio.subtitle_regions.locate',return_value=[]))
        return stack

    def test_actual_export_one_audio_two_captions_shared_visual_unit(self):
        self.draft['product']={'title':'小天鹅（Little Swan）滚筒洗衣机'}
        self.draft['product_name']='模型另起的简称'
        self.studio.store.put_draft(self.draft)
        self.studio.store.meta('current_product',{'title':'另一个正在浏览的商品'})
        with self.patches(),patch('mixstudio.output_names.datetime') as clock:
            clock.now.return_value=datetime(2026,10,24,4,30)
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual([c[0] for c in self.calls],['preflight','fish','alignment','glm','deepseek'])
        result=job['result'];self.assertAlmostEqual(result['duration'],5.,places=1)
        self.assertEqual(len(result['timeline_plan']['segments']),1)
        self.assertEqual(result['voice_speed'],1.)
        self.assertTrue(Path(result['path']).is_file())
        self.assertEqual(Path(result['path']).name,'小天鹅（L 10.24.4.mp4')
        self.assertFalse(list((self.root/'out').glob('*.reserve')))
        self.assertEqual(result['published'],True)
        saved=self.studio.store.draft('d')
        self.assertEqual(saved['whole_audio']['duration'],5.)
        self.assertEqual(len(saved['whole_voice']['captions']),2)
        media._run(['ffmpeg','-v','error','-xerror','-i',result['path'],'-f','null','-'])

    def test_render_failure_retry_does_not_buy_voice_or_matching_again(self):
        with self.patches(),patch.object(media,'render_video',side_effect=ValueError('injected local failure')):
            first=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(first['status'],'failed')
        self.assertFalse(list((self.root/'out').glob('*.reserve')))
        self.calls=[]
        with self.patches(): second=self.wait(self.studio.retry(first['id'],True))
        self.assertEqual(second['status'],'completed',second)
        self.assertNotIn('fish',[c[0] for c in self.calls])
        self.assertNotIn('glm',[c[0] for c in self.calls])
        self.assertNotIn('deepseek',[c[0] for c in self.calls])

    def test_old_failed_v2_job_can_retry_through_new_production_route(self):
        job=self.studio.store.create_job('historical','render',{'draft_id':'d','revision':1})
        self.studio.store.update_job('historical',status='failed')
        with self.patches(): current=self.wait(self.studio.retry_whole_job('historical',True))
        self.assertEqual(current['status'],'completed',current)
        self.assertEqual(current['result']['pipeline'],'whole_voice_v1')
        self.assertEqual(sum(c[0]=='fish' for c in self.calls),1)

    def test_preflight_failure_makes_zero_paid_calls(self):
        with self.patches(),patch.object(self.studio,'_free_preflight',side_effect=ValueError('disk full')):
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'failed');self.assertEqual(self.calls,[])

    def test_alignment_failure_preserves_audio_and_never_calls_vision(self):
        with self.patches(),patch.object(self.studio,'_align_whole',side_effect=ValueError('local alignment failed')):
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'failed')
        self.assertEqual([c[0] for c in self.calls],['preflight','fish'])
        record=self.studio.store.draft('d')['whole_audio']
        self.assertTrue(Path(record['path']).is_file())

    def test_cancel_after_complete_paid_response_preserves_wav(self):
        event=threading.Event()
        def paid(text,path,**kw): wav(path);event.set()
        with patch.object(providers,'synthesize_voice',side_effect=paid):
            audio,*_=self.studio._whole_audio(self.draft,self.studio.settings.public(),event)
        self.assertTrue(audio.is_file());self.assertEqual(_pcm_duration(audio),5.)

    def test_corrupt_voice_cache_stops_without_another_request(self):
        with patch.object(providers,'synthesize_voice',side_effect=self.voice):
            audio,*_=self.studio._whole_audio(self.draft,self.studio.settings.public(),threading.Event())
        wav(audio,4.)
        with patch.object(providers,'synthesize_voice') as paid,self.assertRaises(ValueError):
            self.studio._whole_audio(self.draft,self.studio.settings.public(),threading.Event())
        paid.assert_not_called()

    def test_missing_consent_and_duplicate_submission_are_blocked(self):
        with self.assertRaises(ValueError): self.studio.produce_whole('d',False)
        hold=threading.Event();entered=threading.Event()
        def preflight(*a,**k): entered.set();hold.wait(3)
        try:
            with self.patches(),patch.object(self.studio,'_free_preflight',side_effect=preflight):
                first=self.studio.produce_whole('d',True);entered.wait(2)
                with self.assertRaises(ValueError):self.studio.produce_whole('d',True)
                self.studio.jobs.cancel(first['id']);hold.set();self.wait(first)
        finally:hold.set()

    def test_edit_invalidates_current_audio_but_retains_paid_file(self):
        with self.patches():job=self.wait(self.studio.produce_whole('d',True))
        path=Path(self.studio.store.draft('d')['whole_audio']['path'])
        changed=self.studio.edit_v2('d','看看商品。')
        self.assertNotIn('whole_audio',changed);self.assertNotIn('whole_voice',changed)
        self.assertTrue(path.is_file())

    def test_free_preflight_missing_source_and_config_never_calls_model(self):
        missing={**self.asset,'id':'missing','path':str(self.root/'missing.mp4')}
        self.studio.store.put_asset(missing)
        with patch.object(providers,'synthesize_voice') as paid:
            job=self.wait(self.studio.free_preflight(['missing']))
        self.assertEqual(job['status'],'failed');paid.assert_not_called()

    def test_saved_alignment_cache_is_revalidated_and_skips_worker(self):
        settings=self.studio.settings.public();audio=self.root/'a.wav';wav(audio)
        model=Path(settings['asr_model_path'])
        version=[(n,(model/n).stat().st_size,(model/n).stat().st_mtime_ns) for n in ('model.bin','config.json','tokenizer.json')]
        identity=_key(['word_onsets_v1',_hash(audio),SEGMENTS,str(model),version])
        _atomic(self.studio.root/'voice-cache'/('alignment-'+identity+'.json'),
            {'words':TimingTests().words()})
        with patch.object(providers,'_run_process') as worker:
            result=self.studio._align_whole(self.draft,audio,_hash(audio),5.,settings,threading.Event())
        worker.assert_not_called();self.assertEqual(result['segment_seconds'],[2.7,2.3])

    def test_new_http_routes_need_session_and_audio_is_bound_to_cache(self):
        from mixstudio.http_app import create_app
        app=create_app(self.studio);client=app.test_client()
        self.assertNotEqual(client.post('/api/v2/preflight',json={'asset_ids':['a']}).status_code,200)
        with self.patches():self.assertEqual(self.wait(self.studio.produce_whole('d',True))['status'],'completed')
        client.get('/')
        token=app.config['LOCAL_TOKEN']
        response=client.get('/media/voices/d',headers={'X-Local-Token':token})
        self.assertEqual(response.status_code,200)
        self.assertGreater(len(response.data),44)
        response.close()


if __name__=='__main__': unittest.main()

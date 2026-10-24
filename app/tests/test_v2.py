"""V2 orchestration tests; all paid model requests are mocked."""
import json
from pathlib import Path
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio.product import normalize_jd_link, validate_product, JDWindowReader
from mixstudio.http_app import create_app
from mixstudio.service import Studio, fingerprint
from mixstudio.settings import Settings
from mixstudio.v2 import duration_bounds, validate_duration


PRODUCT = {'sku': '100012345678', 'title': '测试品牌电视 55英寸', 'source': 'https://item.jd.com/100012345678.html', 'observed_at': 123456, 'status': 'read'}
TIMED_TEXT = ('选电视先看看它放进家里的样子。这款测试品牌电视可以先从整体外观开始了解，'
              '留意边框和摆放位置，再结合房间的布局想一想。镜头里展示的是商品外观，'
              '其他功能还要查看商品页面的说明。喜欢这种风格，可以继续了解这款电视。')


class ProductTests(unittest.TestCase):
    def test_native_reader_isolated_bridge_title_and_cancel(self):
        fast_path = patch('mixstudio.product.fetch_public_product', return_value=None)
        fast_path.start()
        self.addCleanup(fast_path.stop)
        class Event:
            def __iadd__(self, callback):
                self.callback = callback
                return self
            def wait(self, timeout): return True
        class Window:
            def __init__(self):
                self.events = SimpleNamespace(closed=Event(), loaded=Event())
                self.destroyed = False
            def get_current_url(self): return PRODUCT['source']
            def evaluate_js(self, code): return {**PRODUCT, 'ready': True}
            def destroy(self): self.destroyed = True
        first = Window()
        reader = JDWindowReader()
        with patch('webview.create_window', return_value=first) as create:
            result = reader(normalize_jd_link(PRODUCT['source']), cancel=threading.Event(), progress=lambda *a:None)
            self.assertEqual(result['sku'], PRODUCT['sku'])
            self.assertIsNone(create.call_args.kwargs['js_api'])
            reader.close();self.assertTrue(first.destroyed)
        second=Window();cancel=threading.Event();cancel.set()
        from mixstudio.jobs import Cancelled
        with patch('webview.create_window', return_value=second), self.assertRaises(Cancelled):
            reader(normalize_jd_link(PRODUCT['source']),cancel=cancel,progress=lambda *a:None)
        self.assertTrue(second.destroyed)
        hanging=Window();entered=threading.Event();release=threading.Event();cancel=threading.Event()
        def block(code):
            entered.set();release.wait(5)
        hanging.evaluate_js=block
        def cancel_later():
            entered.wait(2);cancel.set()
        watcher=threading.Thread(target=cancel_later);watcher.start();began=time.monotonic()
        try:
            with patch('webview.create_window',return_value=hanging),self.assertRaises(Cancelled):
                reader(normalize_jd_link(PRODUCT['source']),cancel=cancel,progress=lambda *a:None)
            self.assertLess(time.monotonic()-began,2)
        finally:
            release.set();watcher.join(2)

    def test_urls_and_same_sku(self):
        for url in ['https://item.jd.com/100012345678.html?utm=x', 'https://item.m.jd.com/product/100012345678.html']:
            self.assertEqual(normalize_jd_link('商品 '+url)['sku'], PRODUCT['sku'])
        self.assertIsNone(normalize_jd_link('https://3.cn/abc123')['sku'])
        for url in ['https://item.jd.com.evil.test/100012345678.html', 'https://evil.test/', 'http://item.jd.com/100012345678.html', 'https://x@item.jd.com/100012345678.html', 'https://item.jd.com:444/100012345678.html']:
            with self.assertRaises(ValueError):
                normalize_jd_link(url)
        self.assertEqual(validate_product(PRODUCT)['observed_at'], PRODUCT['observed_at'])
        with self.assertRaises(ValueError):
            validate_product(PRODUCT, '99999')
        with self.assertRaises(ValueError):
            validate_product({**PRODUCT, 'title': '京东登录'})

    def test_limits_and_v1_preferences_migrate_without_file_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'settings.json'; original = json.dumps({'target_seconds': 120, 'output_count': 20});p.write_text(original)
            s=Settings(d)
            self.assertEqual(s.values['target_seconds'],60);self.assertEqual(s.values['output_count'],5)
            self.assertEqual(p.read_text(),original)
            for value in [{'target_seconds':61},{'output_count':6},{'output_count':True},{'vision_endpoint':'https://evil.test/'},{'vision_model':'anything'}]:
                with self.assertRaises(ValueError):s.update(value)
        self.assertEqual(duration_bounds(60),(.1,60));self.assertEqual(duration_bounds(25),(.1,60))
        for actual in [0,60.01,float('nan')]:
            with self.assertRaises(ValueError):validate_duration(25,actual)
        validate_duration(25,20);validate_duration(25,30)


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.studio=Studio(self.root/'runtime')
        self.studio.settings.update({'output_dir':str(self.root/'out')})
        self.source=self.root/'source.mp4';self.source.write_bytes(b'fixture video')
        self.asset={'id':'a1','name':'source.mp4','path':str(self.source),'sha256':fingerprint(self.source),
                    'duration':30,'width':320,'height':480,'media_info':{'hdr':False},'clips':[],'imported_at':1,'libraries':['local']}
        # This is an orchestration-only fixture, not decodable media. Model the
        # successful local probe explicitly, bound to this exact file version.
        from mixstudio import preprocess
        from mixstudio.media_identity import file_version
        self.asset['prepared_media']={'version':preprocess.VERSION,'mode':'original',
            'source_sha256':self.asset['sha256'],'time_offset':0.,
            'probe_version':preprocess.PROBE_VERSION,'source_version':list(file_version(self.source))}
        self.studio.store.put_asset(self.asset)
        self.studio.store.meta('current_product',PRODUCT)
        self.key=patch.object(self.studio.settings,'secret',return_value='test-placeholder');self.key.start()
        self.network=patch('mixstudio.providers._post',side_effect=AssertionError('No live network permitted in offline tests'))
        self.network.start()

    def tearDown(self):
        self.studio.close();self.network.stop();self.key.stop();self.temp.cleanup()

    def wait(self, job):
        end=time.monotonic()+8
        while time.monotonic()<end:
            current=self.studio.store.job(job['id'])
            if current['status'] in {'completed','failed','cancelled'}:return current
            time.sleep(.01)
        self.fail('Job timed out')

    def generated(self):
        from mixstudio.script_engine import estimate_seconds
        return [{'title':'文案一','voiceover':TIMED_TEXT,
                 'segments':[{'segment_id':'s1','text':TIMED_TEXT,'visual_hint':'电视外观'}],
                 'estimated_seconds':estimate_seconds(TIMED_TEXT)}]

    def make_draft(self):
        from mixstudio import script_engine,providers
        with patch.object(script_engine,'generate_scripts',return_value=self.generated()) as generate, patch.object(providers,'transcribe_video',side_effect=AssertionError('V2 must not require ASR')):
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1'],'target_seconds':25,'count':1,'allow_paid':True}))
            self.assertEqual(job['status'],'completed',job)
            self.assertEqual(generate.call_count,1)
            return self.studio.store.draft(job['result']['draft_ids'][0])

    def scenes(self):
        return [{'clip_id':f'c{i}', 'asset_id':'a1','source_path':str(self.source),'start':i*5.,'end':(i+1)*5.,
                 'text':'电视整体外观','tags':['外观'],'usable':i!=5,'reason':'商品展示','confidence':.9} for i in range(6)]

    def test_script_first_and_deduplicated_selected_assets(self):
        from mixstudio import script_engine
        duplicate={**self.asset,'id':'a2','libraries':['douyin']};self.studio.store.put_asset(duplicate)
        with patch.object(script_engine,'generate_scripts',return_value=self.generated()):
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1','a2'],'allow_paid':True}))
        self.assertEqual(job['status'],'completed')
        draft=self.studio.store.draft(job['result']['draft_ids'][0]);self.assertEqual(draft['asset_ids'],['a1'])
        self.assertEqual(draft['match_status'],'pending');self.assertEqual(draft['clips'],[])
        with self.assertRaises(ValueError):self.studio.render(draft['id'],True)
        state=self.studio.state();self.assertEqual(len(state['assets']),2);self.assertEqual(state['version'],2)

    def test_invalid_generation_before_request(self):
        from mixstudio import script_engine
        with patch.object(script_engine,'generate_scripts') as generate:
            for fields in [{'count':6},{'count':True},{'target_seconds':61},{'allow_paid':False},{'asset_ids':['missing']}]:
                with self.assertRaises(ValueError):self.studio.generate_v2({'asset_ids':['a1'],'allow_paid':True,**fields})
            generate.assert_not_called()

    def test_match_then_edit_invalidates_and_manual_exclusions(self):
        from mixstudio import script_engine,vision
        draft=self.make_draft();scenes=self.scenes()
        selected=['c0','c1','c2','c3','c4']
        result={'matches':[{'segment_id':'s1','clip_ids':selected,'confidence':.9,'reason':'相符'}],'unmatched':[]}
        with patch.object(vision,'analyze_asset',return_value={'scenes':scenes,'warnings':['抽样预览']}),patch.object(script_engine,'match_segments',return_value=result):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        current=self.studio.store.draft(draft['id']);self.assertEqual(current['match_status'],'ready')
        self.assertEqual(self.studio.bound_v2(current)[0]['matched_clip_ids'],selected)
        for ids in [['missing'],['c5']]:
            with self.assertRaises(ValueError):self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':ids}])
        edited=self.studio.edit_v2(draft['id'],'换一段文案。');self.assertEqual(edited['match_status'],'pending')
        with self.assertRaises(ValueError):self.studio.render(draft['id'],True)

    def test_match_progress_forwards_asset_and_exact_vision_batch(self):
        from mixstudio import script_engine,vision
        draft=self.make_draft()
        source2=self.root/'second.mp4';source2.write_bytes(b'second fixture video')
        asset2={**self.asset,'id':'a2','path':str(source2),'sha256':fingerprint(source2)}
        from mixstudio.media_identity import file_version
        asset2['prepared_media']={**self.asset['prepared_media'],'source_sha256':asset2['sha256'],
                                  'source_version':list(file_version(source2))}
        self.studio.store.put_asset(asset2)
        draft['asset_ids']=['a1','a2'];self.studio.store.put_draft(draft)
        def analyze(asset,cache_dir,**kwargs):
            callback=kwargs.get('progress')
            self.assertTrue(callable(callback),'vision progress must reach the job UI')
            callback(0,'正在抽取短片段画面（本视频第 1/2 批；4 个片段 / 12 张图）')
            callback(.5,'正在进行画面识别（本视频第 2/2 批；4 个片段 / 12 张图）')
            callback(1,'画面抽样分析完成，请预览核对')
            return {'scenes':self.scenes() if asset['id']=='a1' else [],'warnings':[]}
        with patch.object(vision,'analyze_asset',side_effect=analyze), \
             patch.object(script_engine,'match_segments',return_value={'matches':[],'unmatched':['s1']}), \
             patch.object(self.studio.store,'update_job',wraps=self.studio.store.update_job) as update:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        events=[c.kwargs for c in update.call_args_list if 'progress' in c.kwargs]
        for label,wanted in (('素材 1/2',.2125),('素材 2/2',.5375)):
            batches=[e for e in events if label in e.get('message','') and '第 2/2 批' in e['message']]
            self.assertEqual(len(batches),1)
            self.assertIn('4 个片段 / 12 张图',batches[0]['message'])
            self.assertAlmostEqual(batches[0]['progress'],wanted)
        self.assertTrue(any('DeepSeek 画面匹配' in e.get('message','') for e in events))

    def test_visual_error_stage_preserves_type_batch_and_hides_raw_message(self):
        from mixstudio import script_engine,vision,providers
        draft=self.make_draft()
        for code,unknown in (('timeout',True),('dns',True),('http_401',False),('cancelled',True),('cache_write',True)):
            with self.subTest(code=code):
                failure=providers.ProviderError('private raw payload test-placeholder',code=code,result_unknown=unknown)
                failure.vision_batch=(2,3,2)
                failure.request_phase='read'
                def inline(kind,payload,work):
                    return work(threading.Event(),lambda *args:None)
                with patch.object(self.studio.jobs,'submit',side_effect=inline), \
                     patch.object(vision,'analyze_asset',side_effect=failure), \
                     patch.object(script_engine,'match_segments') as matcher, \
                     self.assertRaises(providers.ProviderError) as error:
                    self.studio.match_v2(draft['id'],True)
                self.assertEqual(error.exception.code,code)
                self.assertEqual(error.exception.result_unknown,unknown)
                self.assertEqual(error.exception.request_phase,'read')
                self.assertIn('读取响应',str(error.exception))
                self.assertIn('画面识别',str(error.exception))
                self.assertIn('素材 1/1',str(error.exception))
                self.assertIn('本视频第 2/3 批',str(error.exception))
                self.assertIn('2 个片段 / 最多 6 张图',str(error.exception))
                self.assertNotIn('private raw',str(error.exception))
                self.assertNotIn('test-placeholder',str(error.exception))
                self.assertIn('仅已成功写入的批次缓存可复用',str(error.exception))
                self.assertNotIn('已完成批次缓存保留',str(error.exception))
                matcher.assert_not_called()

    def test_matcher_error_stage_preserves_type_and_hides_raw_message(self):
        from mixstudio import script_engine,vision,providers
        draft=self.make_draft()
        for code,unknown in (('timeout',True),('connection',True),('http_401',False),('invalid_response',False)):
            with self.subTest(code=code):
                failure=providers.ProviderError('private response test-placeholder',code=code,result_unknown=unknown)
                failure.request_phase='connect'
                def inline(kind,payload,work):
                    return work(threading.Event(),lambda *args:None)
                with patch.object(self.studio.jobs,'submit',side_effect=inline), \
                     patch.object(vision,'analyze_asset',return_value={'scenes':self.scenes(),'warnings':[]}), \
                     patch.object(script_engine,'match_segments',side_effect=failure), \
                     self.assertRaises(providers.ProviderError) as error:
                    self.studio.match_v2(draft['id'],True)
                self.assertEqual(error.exception.code,code)
                self.assertEqual(error.exception.result_unknown,unknown)
                self.assertEqual(error.exception.request_phase,'connect')
                self.assertIn('连接或等待响应',str(error.exception))
                self.assertIn('DeepSeek 画面匹配',str(error.exception))
                self.assertNotIn('本视频第',str(error.exception))
                self.assertNotIn('private response',str(error.exception))
                self.assertNotIn('test-placeholder',str(error.exception))
                if code=='timeout':self.assertIn('超时',str(error.exception))
                if code=='http_401':self.assertIn('密钥无效',str(error.exception))

    def test_vision_invalid_context_is_not_reflected_in_job_error(self):
        from mixstudio import script_engine,vision,providers
        draft=self.make_draft()
        failure=providers.ProviderError('private raw',code='tls',result_unknown=True)
        failure.vision_batch=('private metadata',3,4)
        failure.request_phase='private phase'
        with patch.object(vision,'analyze_asset',side_effect=failure), \
             patch.object(script_engine,'match_segments') as matcher:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed',job)
        self.assertTrue(job['result_unknown'])
        self.assertIn('画面识别',job['error'])
        self.assertIn('安全连接',job['error'])
        self.assertNotIn('private',job['error'])
        matcher.assert_not_called()

    def test_product_actual_reader_and_no_credentials_in_jobs(self):
        self.studio.product_reader=lambda link,**kwargs:PRODUCT
        job=self.wait(self.studio.read_product(PRODUCT['source']))
        self.assertEqual(job['status'],'completed');self.assertEqual(job['result']['sku'],PRODUCT['sku'])
        self.assertNotIn('test-placeholder',json.dumps(self.studio.state(),ensure_ascii=False))

    def test_download_does_not_switch_local_folder(self):
        self.studio.store.meta('folder',str(self.root))
        from mixstudio import providers
        with patch.object(providers,'download_douyin',return_value={'path':str(self.source),'title':'测试'}),patch.object(self.studio,'_import_one',return_value=self.asset):
            jobs=self.studio.download(['https://v.douyin.com/example/'],str(self.root/'download'))
            job=self.wait(jobs[0])
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(self.studio.state()['folder'],str(self.root))
        self.assertEqual(self.studio.store.asset('a1')['libraries'],['douyin','local'])

    def test_visual_budget_allows_4500_and_rejects_too_many_assets_before_paid(self):
        from mixstudio import vision
        draft=self.make_draft();ids=[]
        for i in range(51):
            asset={**self.asset,'id':f'long{i}','sha256':f'{i:064x}','duration':270}
            self.studio.store.put_asset(asset);ids.append(asset['id'])
        draft['asset_ids']=ids;self.studio.store.put_draft(draft)
        with patch.object(vision,'analyze_asset') as paid, self.assertRaisesRegex(ValueError,'1～50'):
            self.studio.match_v2(draft['id'],True)
        paid.assert_not_called()
        self.assertEqual(sum(min(90, __import__('math').ceil(270/3)) for _ in ids[:50]),4500)

    def test_http_v2_endpoints_and_legacy_library_labels(self):
        from mixstudio import script_engine
        client=create_app(self.studio,token='offline-v2').test_client()
        headers={'X-Local-Token':'offline-v2'}
        self.assertEqual(client.post('/api/v2/generate',json={}).status_code,403)
        self.assertEqual(client.post('/api/v2/generate',headers=headers,json=[]).status_code,400)
        self.studio.product_reader=lambda *a,**k:PRODUCT
        read=client.post('/api/product/read',headers=headers,json={'url':PRODUCT['source']})
        self.assertEqual(read.status_code,200);self.assertEqual(self.wait(read.json['data'])['status'],'completed')
        with patch.object(script_engine,'generate_scripts',return_value=self.generated()):
            response=client.post('/api/v2/generate',headers=headers,json={'asset_ids':['a1'],'allow_paid':True})
            job=self.wait(response.json['data'])
        identifier=job['result']['draft_ids'][0]
        response=client.put('/api/v2/drafts/'+identifier,headers=headers,json={'voiceover':'展示商品外观。'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['data']['match_status'],'pending')
        self.assertEqual(client.post('/api/v2/drafts/'+identifier+'/match',headers=headers,json={'allow_paid':False}).status_code,400)
        saved=client.put('/api/v2/drafts/'+identifier+'/matches',headers=headers,json={'matches':[]})
        self.assertEqual(saved.status_code,200)
        self.assertEqual(saved.json['data']['match_status'],'needs_review')
        asset=dict(self.asset);asset.pop('libraries');self.studio.store.put_asset(asset)
        self.studio.store.create_job('legacy-download','download',{})
        self.studio.store.update_job('legacy-download',status='completed',result={'asset_id':'a1'})
        self.assertEqual(self.studio.state()['assets'][0]['libraries'],['douyin'])
        self.studio._mark_library(asset,'local')
        self.assertEqual(self.studio.store.asset('a1')['libraries'],['douyin','local'])

    def test_running_guard_not_lost_after_200_new_jobs(self):
        draft=self.make_draft()
        self.studio.store.create_job('long-match','match',{'draft_id':draft['id']})
        self.studio.store.update_job('long-match',status='running')
        for i in range(205):
            self.studio.store.create_job(f'done{i}','scan',{})
            self.studio.store.update_job(f'done{i}',status='completed')
        self.assertIn('long-match',[j['id'] for j in self.studio.store.jobs()])
        self.assertTrue(self.studio._draft_busy(draft['id']))
        with self.assertRaises(ValueError):self.studio.edit_v2(draft['id'],'不应被保存。')
        self.assertEqual(self.studio.store.draft(draft['id'])['voiceover'],draft['voiceover'])

    def test_real_generation_preserves_mixed_timing_batch_with_one_http_request(self):
        from mixstudio import providers
        texts=[TIMED_TEXT, '测试品牌电视，先看看外观。', TIMED_TEXT+'然后继续看看整体外观和摆放细节。'*4]
        raw={'drafts':[{'title':f'文案{label}','voiceover':text,
                       'segments':[{'segment_id':'s1','text':text,'visual_hint':'同款电视整体外观',
                                    'fact_quotes':['测试品牌电视']}]} for label,text in zip('甲乙丙',texts)]}
        envelope=json.dumps({'choices':[{'finish_reason':'stop','message':{
            'content':json.dumps(raw,ensure_ascii=False)}}]},ensure_ascii=False).encode()
        with patch.object(providers,'_post',return_value=(envelope,'application/json')) as paid:
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1'],'target_seconds':25,'count':3,'allow_paid':True}))
        self.assertEqual(job['status'],'completed',job)
        identifiers=job['result']['draft_ids']
        self.assertEqual(len(identifiers),3)
        drafts=[self.studio.store.draft(identifier) for identifier in identifiers]
        self.assertEqual([d['voiceover'] for d in drafts],texts)
        self.assertEqual([d['timing_status'] for d in drafts],['ready','ready','ready'])
        self.assertEqual(drafts[-1]['timing_mode'],'normal')
        self.assertEqual(job['result']['needs_adjustment_ids'],[])
        for draft in drafts:
            self.assertEqual(''.join(s['text'] for s in draft['segments']),draft['voiceover'])
        paid.assert_called_once()

    def test_generation_recomputes_timing_instead_of_trusting_model_estimate(self):
        from mixstudio import script_engine
        fabricated=self.generated()[0]
        fabricated.update(voiceover='看看电视。',estimated_seconds=25,timing_status='ready',spoken_units=100)
        fabricated['segments'][0]['text']=fabricated['voiceover']
        with patch.object(script_engine,'generate_scripts',return_value=[fabricated]) as generate:
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1'],'allow_paid':True}))
        self.assertEqual(job['status'],'completed',job)
        draft=self.studio.store.draft(job['result']['draft_ids'][0])
        self.assertEqual(draft['voiceover'],fabricated['voiceover'])
        self.assertEqual(draft['estimated_seconds'],1)
        self.assertEqual(draft['spoken_units'],4)
        self.assertEqual(draft['timing_status'],'ready')
        self.assertEqual(job['result']['needs_adjustment_ids'],[])
        generate.assert_called_once()

    def test_empty_copy_blocks_match_before_any_paid_requests_even_with_forged_ready(self):
        from mixstudio import script_engine,vision,providers
        draft=self.make_draft()
        draft.update(voiceover='',estimated_seconds=25,timing_status='ready')
        draft['segments'][0]['text']=draft['voiceover']
        self.studio.store.put_draft(draft)
        with patch.object(vision,'analyze_asset',return_value={'scenes':[],'warnings':[]}) as visual, \
             patch.object(script_engine,'match_segments',return_value={'matches':[],'unmatched':['s1']}) as matcher, \
             patch.object(providers,'_post',side_effect=AssertionError('No live network permitted')) as paid:
            with self.assertRaises(ValueError):
                job=self.studio.match_v2(draft['id'],True)
                self.wait(job)
            visual.assert_not_called();matcher.assert_not_called();paid.assert_not_called()

    def test_nonspoken_copy_blocks_bound_and_preflight_using_legacy_segment_fallback(self):
        from mixstudio import media
        for has_voiceover in (True,False):
            with self.subTest(has_voiceover=has_voiceover):
                draft=self.make_draft()
                draft.update(clips=self.scenes(),match_status='ready',timing_status='ready',estimated_seconds=25,
                    matches=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}])
                draft['segments'][0]['text']='。！？'
                if has_voiceover:
                    draft['voiceover']='。！？'
                else:
                    draft.pop('voiceover',None)
                with self.assertRaises(ValueError):self.studio.bound_v2(draft)
                draft['segments'][0].update(clip_id='c0',matched_clip_ids=['c0','c1','c2','c3','c4'])
                with patch.object(media,'_plan') as plan, self.assertRaises(ValueError):
                    self.studio.preflight_v2(draft,self.studio.settings.public())
                plan.assert_not_called()

    def test_short_edit_is_ready_and_matching_not_blocked_by_reference(self):
        from mixstudio import script_engine,vision,providers
        draft=self.make_draft()
        adjusted=self.studio.edit_v2(draft['id'],'看看电视。')
        self.assertEqual(adjusted['timing_status'],'ready')
        self.assertEqual(adjusted['spoken_units'],4)
        self.assertEqual(adjusted['match_status'],'pending')
        self.assertTrue(adjusted['timing_message'])
        with patch.object(vision,'analyze_asset',return_value={'scenes':[],'warnings':[]}) as visual, \
             patch.object(providers,'_post',side_effect=AssertionError('No live requests')) as paid:
            job=self.wait(self.studio.match_v2(draft['id'],True))
            self.assertEqual(job['status'],'completed',job)
            visual.assert_called_once();paid.assert_not_called()
        restored=self.studio.edit_v2(draft['id'],TIMED_TEXT)
        self.assertEqual(restored['timing_status'],'ready')
        self.assertEqual(restored['estimated_seconds'],24)
        self.assertIn('参考',restored['timing_message'])
        self.assertEqual(restored['match_status'],'pending')
        self.assertEqual(restored['matches'],[])
        with patch.object(vision,'analyze_asset',return_value={'scenes':self.scenes(),'warnings':[]}) as visual, \
             patch.object(script_engine,'match_segments',return_value={'matches':[],
                 'unmatched':[s['segment_id'] for s in restored['segments']]}) as matcher:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        visual.assert_called_once();matcher.assert_called_once()

    def test_http_rejects_empty_copy_match_and_render_without_creating_jobs(self):
        from mixstudio import providers
        draft=self.make_draft()
        draft.update(voiceover='',estimated_seconds=25,timing_status='ready',
                     clips=self.scenes(),match_status='ready',
                     matches=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}])
        draft['segments'][0]['text']=draft['voiceover']
        self.studio.store.put_draft(draft)
        client=create_app(self.studio,token='timing-offline').test_client()
        headers={'X-Local-Token':'timing-offline'}
        count=len(self.studio.store.jobs())
        with patch.object(providers,'_post',side_effect=AssertionError('No paid request permitted')) as paid:
            match=client.post(f'/api/v2/drafts/{draft["id"]}/match',headers=headers,json={'allow_paid':True})
            render=client.post(f'/api/drafts/{draft["id"]}/render',headers=headers,json={'allow_paid':True})
            self.assertEqual(match.status_code,400,match.json)
            self.assertEqual(render.status_code,400,render.json)
            self.assertEqual(len(self.studio.store.jobs()),count)
            paid.assert_not_called()

    def test_actual_audio_keeps_final_ceiling_but_not_reference_interval(self):
        from mixstudio import script_engine
        self.assertEqual(script_engine.timing_fields(TIMED_TEXT,25)['timing_status'],'ready')
        for target,actual in ((25,0),(25,60.001),(60,.099),(60,60.001)):
            with self.subTest(target=target,actual=actual),self.assertRaises(ValueError):
                validate_duration(target,actual)
        for target,actual in ((25,19.999),(25,30.001),(60,54.999),(60,60)):
            validate_duration(target,actual)

    def test_old_valid_draft_without_voiceover_uses_actual_segments_not_stale_estimate(self):
        draft=self.make_draft()
        draft.update(clips=self.scenes(),match_status='ready',estimated_seconds=100,
                     timing_status='needs_adjustment',
                     matches=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}])
        draft.pop('voiceover')
        bound=self.studio.bound_v2(draft)
        self.assertEqual(''.join(s['text'] for s in bound),TIMED_TEXT)
        self.assertEqual(bound[0]['matched_clip_ids'],['c0','c1','c2','c3','c4'])

    def test_equal_length_but_mismatched_voiceover_and_segments_is_rejected(self):
        draft=self.make_draft()
        draft.update(clips=self.scenes(),match_status='ready',
                     matches=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}])
        draft['voiceover']=draft['voiceover'].replace('电视','冰箱')
        with self.assertRaisesRegex(ValueError,'不一致'):
            self.studio.bound_v2(draft)

    def test_speed_adaptable_edit_and_existing_saved_copy_enter_mock_matching(self):
        from mixstudio import script_engine, vision
        draft = self.make_draft()
        text = '看' * 122
        saved = self.studio.edit_v2(draft['id'], text)
        self.assertEqual(saved['timing_status'], 'ready')
        self.assertEqual(saved['timing_mode'], 'normal')
        self.assertEqual(saved['estimated_voice_speed'], 1.)
        # Old persisted flags must not demand a fresh save or model generation.
        saved.update(timing_status='needs_adjustment', timing_mode='needs_adjustment')
        self.studio.store.put_draft(saved)
        with patch.object(vision, 'analyze_asset', return_value={'scenes': self.scenes(), 'warnings': []}) as visual, \
             patch.object(script_engine, 'match_segments', return_value={'matches': [], 'unmatched': ['s1']}) as matcher:
            job = self.wait(self.studio.match_v2(saved['id'], True))
        self.assertEqual(job['status'], 'completed', job)
        visual.assert_called_once()
        matcher.assert_called_once()
        self.assertEqual(self.studio.store.draft(saved['id'])['voiceover'], text)

    def test_state_recomputes_speed_readiness_without_writing_drafts_or_task_history(self):
        draft = self.make_draft()
        text = '看' * 122
        draft.update(voiceover=text, segments=[{'segment_id': 's1', 'text': text, 'visual_hint': '同商品'}],
                     timing_status='needs_adjustment', estimated_seconds=99)
        self.studio.store.put_draft(draft)
        self.studio.store.create_job('legacy-length', 'script', {})
        self.studio.store.update_job('legacy-length', status='completed',
            result={'draft_ids': [draft['id']], 'needs_adjustment_ids': [draft['id']]})
        jobs_before = self.studio.store.jobs()
        with patch.object(self.studio.store, 'put_draft', side_effect=AssertionError('Response-only refresh')):
            state = self.studio.state()
        public = next(item for item in state['drafts'] if item['id'] == draft['id'])
        self.assertEqual(public['timing_status'], 'ready')
        self.assertEqual(public['timing_mode'], 'normal')
        self.assertEqual(public['estimated_seconds'], 30.5)
        self.assertEqual(self.studio.store.draft(draft['id']), draft)
        self.assertEqual(self.studio.store.jobs(), jobs_before)
        historical = next(job for job in state['jobs'] if job['id'] == 'legacy-length')
        self.assertEqual(historical['result']['needs_adjustment_ids'], [draft['id']])

    def test_invalid_legacy_timing_does_not_break_state_polling(self):
        draft = self.make_draft()
        for changes in ({'target_seconds': '25'}, {'voiceover': 12}, {'target_seconds': None}):
            with self.subTest(changes=changes):
                bad = {**draft, **changes}
                self.studio.store.put_draft(bad)
                public = next(item for item in self.studio.state()['drafts'] if item['id'] == draft['id'])
                self.assertEqual(public['timing_status'], 'needs_adjustment')
                self.assertEqual(self.studio.store.draft(draft['id']), bad)


if __name__=='__main__':unittest.main()

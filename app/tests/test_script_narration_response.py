"""Simple desktop copy contract; all paid responses are offline fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine as engine, vision
from mixstudio.service import Studio, fingerprint


PRODUCT = {'sku':'10237633160811','title':'海尔云溪5.0 12KG滚筒洗衣机 超薄嵌入 双效除菌',
           'source':'https://item.jd.com/10237633160811.html','status':'read','observed_at':1}
COPIES = [
    {'title':'大容量选择','voiceover':'选洗衣机，可以先看看日常需要处理多少衣物。这款海尔云溪5.0，标题标注12KG，具体规格可以继续查看商品详情。'},
    {'title':'嵌入空间','voiceover':'准备规划洗衣区，可以留意这款海尔洗衣机。标题写了超薄嵌入，选购前结合家里的实际空间，查看商品详情中的安装说明。'},
    {'title':'功能关注','voiceover':'了解这款海尔洗衣机时，可以关注标题中的双效除菌。先把型号和具体功能看清楚，再结合自己的日常需求来选择。'},
]


def wire(value):
    return json.dumps({'choices':[{'finish_reason':'stop','message':{
        'content':json.dumps(value,ensure_ascii=False)}}]},ensure_ascii=False).encode(), 'application/json'


class NarrationResponseTests(unittest.TestCase):
    def generate(self, value, **kwargs):
        options = {'api_key':'offline-placeholder','model':'deepseek-flash',
                   'target_seconds':35,'count':3,'narration_only':True,**kwargs}
        with patch.object(providers,'_post',return_value=wire(value)) as post:
            result = engine.generate_scripts(PRODUCT,**options)
        post.assert_called_once()
        return result,post.call_args

    def test_legacy_contract_reproduces_the_screenshot_for_readable_copy(self):
        with patch.object(providers,'_post',return_value=wire({'drafts':COPIES})) as post:
            with self.assertRaises(providers.ProviderError) as caught:
                engine.generate_scripts(PRODUCT,api_key='offline-placeholder',model='deepseek-flash',
                                        target_seconds=35,count=3)
        self.assertEqual(str(caught.exception),'模型返回结果未通过校验，请检查后手动重试。')
        self.assertEqual(caught.exception.code,'invalid_response')
        post.assert_called_once()

    def test_three_plain_narrations_get_local_segments_from_one_request(self):
        result,args = self.generate({'drafts':COPIES})
        self.assertEqual([d['voiceover'] for d in result],[d['voiceover'] for d in COPIES])
        for item in result:
            self.assertEqual(''.join(s['text'] for s in item['segments']),item['voiceover'])
            self.assertEqual([s['segment_id'] for s in item['segments']],
                             [f's{i}' for i in range(1,len(item['segments'])+1)])
            engine._segments_for_match(item['segments'])
        prompt = args.args[1]['messages'][0]['content']
        self.assertIn('软目标',prompt)
        self.assertIn('"voiceover"',prompt)
        self.assertNotIn('"fact_quotes"',prompt)
        self.assertNotIn('"segment_id"',prompt)

    def test_old_internal_metadata_is_not_a_desktop_copy_gate(self):
        copies = copy.deepcopy(COPIES)
        for draft in copies:
            draft.update(estimated_seconds=999,segments=[{'segment_id':'s0','fact_quotes':None,
                'source_path':'C:/ignored/never-used.mp4','text':'与口播不同的内部文字'}])
        before = copy.deepcopy(copies)
        result,_ = self.generate({'drafts':copies,'usage_note':'ignored metadata'})
        self.assertEqual(copies,before)
        self.assertEqual([d['voiceover'] for d in result],[d['voiceover'] for d in COPIES])
        self.assertNotIn('never-used',json.dumps(result))
        self.assertNotIn('与口播不同',json.dumps(result,ensure_ascii=False))
        self.assertTrue(all(d['estimated_seconds'] != 999 for d in result))

    def test_missing_display_title_is_locally_derived(self):
        copies = copy.deepcopy(COPIES);copies[0].pop('title')
        result,_ = self.generate({'drafts':copies})
        self.assertEqual(result[0]['title'],COPIES[0]['voiceover'][:20])

    def test_copy_count_mismatch_has_specific_field_and_counts(self):
        with self.assertRaises(providers.ProviderError) as caught:
            self.generate({'drafts':COPIES[:2]})
        self.assertEqual(caught.exception.code,'invalid_response')
        self.assertEqual(caught.exception.diagnostic['script_detail'],[0,'drafts','count'])
        self.assertEqual(caught.exception.diagnostic['script_counts'],[3,2])
        self.assertIn('3',str(caught.exception));self.assertIn('2',str(caught.exception))

    def test_invalid_spoken_text_is_identified_without_remote_content_in_error(self):
        for value,rule in ((None,'text'),('', 'empty'),('看'*2001,'length'),
                           ('内容\x00PRIVATE','control'),('https://private.invalid/SECRET','fields')):
            copies = copy.deepcopy(COPIES);copies[1]['voiceover'] = value
            with self.subTest(rule=rule), self.assertRaises(providers.ProviderError) as caught:
                self.generate({'drafts':copies})
            self.assertEqual(caught.exception.code,'invalid_response')
            self.assertEqual(caught.exception.diagnostic['script_detail'],[2,'voiceover',rule])
            self.assertNotIn('PRIVATE',str(caught.exception));self.assertNotIn('SECRET',str(caught.exception))

    def test_duplicate_narration_versions_remain_an_error(self):
        copies = copy.deepcopy(COPIES);copies[2] = copy.deepcopy(copies[0])
        with self.assertRaises(providers.ProviderError) as caught:
            self.generate({'drafts':copies})
        self.assertEqual(caught.exception.diagnostic['script_detail'],[3,'voiceover','duplicate'])

    def test_long_unpunctuated_copy_is_split_before_paid_downstream_stages(self):
        for text in ('看'*500+'。','看'*1200+'。','。。'+COPIES[0]['voiceover']):
            with self.subTest(length=len(text)):
                copies = copy.deepcopy(COPIES);copies[0]['voiceover'] = text
                result,_ = self.generate({'drafts':copies})
                segments = result[0]['segments']
                self.assertEqual(''.join(s['text'] for s in segments),text)
                self.assertTrue(all(len(s['text'])<=500 for s in segments))
                self.assertTrue(all(engine.estimate_seconds(s['text'])>0 for s in segments))
                engine._segments_for_match(segments)

    def test_unicode_whitespace_and_punctuation_are_preserved_in_spoken_copy(self):
        copies = copy.deepcopy(COPIES)
        text = '看看海尔云溪5.0。\n再留意 12KG，结合需求选择！'
        copies[0]['voiceover'] = text
        result,_ = self.generate({'drafts':copies})
        self.assertEqual(result[0]['voiceover'],text)
        self.assertEqual(''.join(s['text'] for s in result[0]['segments']),text)

    def test_reference_deviation_and_claim_flags_do_not_block_valid_copy(self):
        copies = copy.deepcopy(COPIES);copies[0]['voiceover'] += '亲测最好，优惠999元。'
        result,_ = self.generate({'drafts':copies})
        self.assertEqual(result[0]['voiceover'],copies[0]['voiceover'])
        self.assertEqual(result[0]['fact_status'],'needs_review')
        self.assertEqual(result[0]['timing_status'],'ready')

    def test_invalid_mode_rejects_before_any_request(self):
        with patch.object(providers,'_post') as post:
            with self.assertRaises(providers.ProviderError):
                engine.generate_scripts(PRODUCT,api_key='offline-placeholder',model='deepseek-flash',
                    target_seconds=35,count=3,narration_only='yes')
        post.assert_not_called()


class DesktopNarrationWorkflowTests(unittest.TestCase):
    def test_actual_voice_first_orchestrator_stores_three_plain_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);source = root/'fixture.mp4';source.write_bytes(b'local fixture')
            studio = Studio(root/'runtime')
            try:
                studio.store.put_asset({'id':'a','path':str(source),'name':source.name,'duration':9,
                    'sha256':fingerprint(source),'imported_at':1,'media_info':{'hdr':False},'libraries':['local']})
                studio.store.meta('current_product',PRODUCT)
                with patch.object(studio.settings,'secret',return_value='offline-placeholder'), \
                     patch.object(studio,'_free_preflight'), patch.object(studio,'_unchanged'), \
                     patch.object(providers,'_post',return_value=wire({'drafts':COPIES})) as post, \
                     patch.object(providers,'synthesize_voice') as fish, patch.object(vision,'analyze_asset') as glm:
                    job = studio.generate_v2({'asset_ids':['a'],'target_seconds':35,'count':3,
                                              'allow_paid':True,'voice_first':True})
                    deadline = time.monotonic()+5
                    while time.monotonic()<deadline:
                        current = studio.store.job(job['id'])
                        if current['status'] in ('completed','failed','cancelled'):
                            break
                        time.sleep(.01)
                    self.assertEqual(current['status'],'completed',current)
                    drafts = [studio.store.draft(identifier) for identifier in current['result']['draft_ids']]
                    self.assertEqual(len(drafts),3)
                    self.assertTrue(all(d['production_mode']=='whole_voice_v1' for d in drafts))
                    self.assertTrue(all(d['match_status']=='pending' for d in drafts))
                    self.assertEqual([d['voiceover'] for d in drafts],[d['voiceover'] for d in COPIES])
                    post.assert_called_once();fish.assert_not_called();glm.assert_not_called()
            finally:
                studio.close()


if __name__ == '__main__':
    unittest.main()

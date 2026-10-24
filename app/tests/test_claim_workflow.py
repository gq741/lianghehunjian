"""No mandatory copy review; technical/paid gates remain. APIs are mocked."""
import copy
import json
import unittest
from unittest.mock import patch
import test_v2 as fixtures
from mixstudio import script_engine, providers, vision, media
from mixstudio.http_app import create_app


class NoFactGateWorkflowTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    tearDown = fixtures.V2Tests.tearDown
    wait = fixtures.V2Tests.wait
    generated = fixtures.V2Tests.generated
    make_draft = fixtures.V2Tests.make_draft
    scenes = fixtures.V2Tests.scenes

    def pending(self):
        draft = self.make_draft()
        draft.update(fact_status='needs_review', fact_issues=[{
            'field':'voiceover','segment_id':'s1','term':'无线',
            'message':'标题未支持无线，请修改。'}])
        self.studio.store.put_draft(draft)
        return draft

    def matched(self, draft):
        draft=copy.deepcopy(draft)
        draft.update(clips=self.scenes(),match_status='ready',matches=[{
            'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4'],
            'confidence':.9,'reason':'离线测试'}])
        self.studio.store.put_draft(draft)
        return draft

    def test_generation_saves_copy_without_fact_review_gate(self):
        generated = self.generated()
        generated[0].update(fact_status='needs_review', fact_issues=[{
            'field':'voiceover','segment_id':'s1','term':'无线','message':'标题未支持无线。'}])
        with patch.object(script_engine,'generate_scripts',return_value=generated) as model:
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1'],'allow_paid':True}))
        self.assertEqual(job['status'],'completed',job)
        identifier=job['result']['draft_ids'][0]
        self.assertEqual(job['result']['needs_fact_review_ids'],[])
        draft=self.studio.store.draft(identifier)
        self.assertEqual(draft['voiceover'],generated[0]['voiceover'])
        self.assertEqual(draft['fact_status'],'not_required')
        self.assertEqual(draft['fact_issues'],[])
        self.assertEqual(generated[0]['fact_status'],'needs_review')  # Model result is not mutated.
        self.assertEqual(model.call_count,1)

    def test_real_engine_mock_response_is_saved_without_paid_retry(self):
        voice=fixtures.TIMED_TEXT+'支持无线连接。'
        response={'drafts':[{'title':'电视外观','voiceover':voice,'segments':[{
            'segment_id':'s1','text':voice,'visual_hint':'电视外观',
            'fact_quotes':['测试品牌电视']}]}]}
        wire=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(response)}}]}).encode()
        with patch.object(providers,'_post',return_value=(wire,'application/json')) as paid:
            job=self.wait(self.studio.generate_v2({'asset_ids':['a1'],'allow_paid':True}))
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(paid.call_count,1)
        draft=self.studio.store.draft(job['result']['draft_ids'][0])
        self.assertEqual(draft['voiceover'],voice)
        self.assertEqual(draft['fact_status'],'not_required')
        self.assertEqual(draft['fact_issues'],[])
        self.assertEqual(job['result']['needs_fact_review_ids'],[])

    def test_old_pending_metadata_does_not_block_http_matching_or_manual_choices(self):
        draft=self.pending()
        client=create_app(self.studio,token='claims-offline').test_client()
        headers={'X-Local-Token':'claims-offline'}
        chosen=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4'],
                 'confidence':.9,'reason':'离线测试'}]
        with patch.object(providers,'_post') as paid, \
             patch.object(vision,'analyze_asset',return_value={'scenes':self.scenes(),'warnings':[]}) as visual, \
             patch.object(script_engine,'match_segments',return_value={'matches':chosen,'unmatched':[]}) as matcher:
            response=client.post(f'/api/v2/drafts/{draft["id"]}/match',headers=headers,json={'allow_paid':True})
            self.assertEqual(response.status_code,200,response.json)
            job=self.wait(response.json['data'])
            self.assertEqual(job['status'],'completed',job)
            response=client.put(f'/api/v2/drafts/{draft["id"]}/matches',headers=headers,json={'matches':chosen})
            self.assertEqual(response.status_code,200,response.json)
            visual.assert_called_once();matcher.assert_called_once();paid.assert_not_called()
        stored=self.studio.store.draft(draft['id'])
        self.assertEqual(stored['match_status'],'ready')
        self.assertEqual(stored['fact_status'],'needs_review')  # No database migration required.
        self.assertEqual(stored['fact_issues'],draft['fact_issues'])

    def test_bound_and_preflight_ignore_old_fact_flags_but_keep_duration_checks(self):
        draft=self.matched(self.pending())
        before=copy.deepcopy(draft)
        bound=self.studio.bound_v2(draft)
        self.assertEqual(bound[0]['matched_clip_ids'],['c0','c1','c2','c3','c4'])
        prepared={**draft,'segments':bound}
        with patch.object(providers,'_post') as paid,patch.object(media,'_window') as plan:
            self.studio.preflight_v2(prepared,self.studio.settings.public())
            self.assertEqual(plan.call_count,5);paid.assert_not_called()
        with patch.object(media,'_window',side_effect=media.MediaError('所选画面时长不足')) as plan:
            with self.assertRaisesRegex(media.MediaError,'时长不足'):
                self.studio.preflight_v2(prepared,self.studio.settings.public())
            plan.assert_called_once()
        self.assertEqual(draft,before)
        empty_copy={**draft,'voiceover':'','segments':[{'segment_id':'s1','text':'','visual_hint':'外观'}]}
        with self.assertRaises(ValueError):self.studio.bound_v2(empty_copy)

    def test_edit_does_not_audit_and_preserves_title_but_invalidates_matching(self):
        draft=self.matched(self.pending())
        draft['title']='用户原有标题'
        draft['fact_issues'][0].update(field='title',segment_id='')
        self.studio.store.put_draft(draft)
        with patch.object(providers,'_post') as paid, \
             patch.object(script_engine,'review_copy',side_effect=AssertionError('copy audit must not run')) as audit:
            result=self.studio.edit_v2(draft['id'],fixtures.TIMED_TEXT+'支持无线连接。')
        paid.assert_not_called();audit.assert_not_called()
        self.assertEqual(result['fact_status'],'not_required')
        self.assertEqual(result['fact_issues'],[])
        self.assertEqual(result['title'],'用户原有标题')
        self.assertIn('无线连接',result['voiceover'])
        self.assertEqual(''.join(s['text'] for s in result['segments']),result['voiceover'])
        self.assertEqual(result['revision'],draft['revision']+1)
        self.assertEqual(result['match_status'],'pending')
        self.assertEqual(result['matches'],[])
        self.assertIn('timing_status',result)

    def test_invalid_fact_metadata_ignored_but_technical_and_source_checks_remain(self):
        draft=self.matched(self.pending())
        for state in ({'fact_status':'other'},{'fact_status':None},{'fact_issues':{}},
                      {'fact_status':'ready','fact_issues':[{'message':'unresolved'}]}):
            with self.subTest(state=state):
                self.assertTrue(self.studio.bound_v2({**draft,**state}))
        for changes in ({'voiceover':'与段落不一致'},{'matches':[]},{'match_status':'pending'},
                        {'matches':[{'segment_id':'s1','clip_ids':['c0','c0']}]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.studio.bound_v2({**draft,**changes})
        self.source.write_bytes(b'changed fixture source')
        with patch.object(vision,'analyze_asset') as visual,patch.object(script_engine,'match_segments') as matcher:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed',job)
        visual.assert_not_called();matcher.assert_not_called()

    def test_paid_confirmation_is_still_required_before_any_job_or_model(self):
        draft=self.matched(self.pending())
        before=len(self.studio.store.jobs())
        with patch.object(providers,'_post') as paid,patch.object(vision,'analyze_asset') as visual, \
             patch.object(script_engine,'generate_scripts') as generate:
            for confirmed in (False,None,1,'true'):
                with self.subTest(confirmed=confirmed):
                    with self.assertRaises(ValueError):self.studio.generate_v2({'asset_ids':['a1'],'allow_paid':confirmed})
                    with self.assertRaises(ValueError):self.studio.match_v2(draft['id'],confirmed)
                    with self.assertRaises(ValueError):self.studio.render(draft['id'],confirmed)
            paid.assert_not_called();visual.assert_not_called();generate.assert_not_called()
        self.assertEqual(len(self.studio.store.jobs()),before)

    def test_http_edit_ignores_audit_overrides_and_still_rejects_invalid_text(self):
        draft=self.pending()
        client=create_app(self.studio,token='claims-offline').test_client()
        headers={'X-Local-Token':'claims-offline'}
        with patch.object(script_engine,'review_copy',side_effect=AssertionError('no audit')) as audit, \
             patch.object(providers,'_post') as paid:
            response=client.put(f'/api/v2/drafts/{draft["id"]}',headers=headers,
                json={'voiceover':fixtures.TIMED_TEXT+'支持无线连接。','fact_status':'ready','fact_issues':[{'message':'injected'}]})
            self.assertEqual(response.status_code,200,response.json)
            result=self.studio.store.draft(draft['id'])
            self.assertEqual(result['fact_status'],'not_required')
            self.assertEqual(result['fact_issues'],[])
            for text in ('','\x00invalid','字'*2001):
                response=client.put(f'/api/v2/drafts/{draft["id"]}',headers=headers,json={'voiceover':text})
                self.assertEqual(response.status_code,400,response.json)
            audit.assert_not_called();paid.assert_not_called()
        self.assertEqual(self.studio.store.draft(draft['id']),result)


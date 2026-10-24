"""Offline service contract: candidates != allocation, and no new paid retries."""
import copy
import unittest
from unittest.mock import patch
from mixstudio import providers, media, vision, script_engine
import test_v2 as fixtures

class AutoTimelineServiceTests(unittest.TestCase):
    setUp=fixtures.V2Tests.setUp
    tearDown=fixtures.V2Tests.tearDown
    wait=fixtures.V2Tests.wait
    generated=fixtures.V2Tests.generated
    make_draft=fixtures.V2Tests.make_draft
    scenes=fixtures.V2Tests.scenes
    def ready_draft(self):
        draft=self.make_draft()
        draft.update(clips=self.scenes(),match_status='ready',matches=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}])
        self.studio.store.put_draft(draft)
        return draft
    def test_legacy_state_calculates_response_plan_without_store_write_or_api(self):
        draft=self.ready_draft();before=copy.deepcopy(draft)
        with patch.object(providers,'_post') as paid,patch.object(self.studio.store,'put_draft',wraps=self.studio.store.put_draft) as put:
            first=self.studio.state()['drafts'][0]
            second=self.studio.state()['drafts'][0]
        self.assertEqual(first['timeline_plan']['status'],'ready')
        self.assertEqual(first['timeline_plan'],second['timeline_plan'])
        self.assertEqual(self.studio.store.draft(draft['id']),before);put.assert_not_called();paid.assert_not_called()
    def test_single_short_manual_choice_not_ready_and_unrelated_not_used(self):
        draft=self.ready_draft()
        saved=self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':['c0']}])
        self.assertEqual(saved['match_status'],'needs_review')
        self.assertEqual(saved['timeline_plan']['status'],'shortage')
        self.assertEqual({w['clip_id'] for w in saved['timeline_plan']['windows']},{'c0'})
        self.assertTrue(saved['timeline_plan']['shortages'])
    def test_slight_shortfall_resolved_by_bounded_reuse_without_paid_call(self):
        draft=self.ready_draft()
        with patch.object(providers,'_post') as paid:
            saved=self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3']}])
        self.assertEqual(saved['timeline_plan']['status'],'ready')
        self.assertEqual(saved['timeline_plan']['speed'],1)
        self.assertGreater(saved['timeline_plan']['repeat_count'],0)
        paid.assert_not_called()
    def test_manual_save_replaces_old_candidates_and_empty_pool_is_shortage(self):
        draft=self.ready_draft()
        draft['match_candidates']=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}]
        self.studio.store.put_draft(draft)
        saved=self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':['c1']}])
        self.assertEqual(saved['match_candidates'][0]['clip_ids'],['c1'])
        self.assertEqual({w['clip_id'] for w in saved['timeline_plan']['windows']},{'c1'})
        self.assertEqual(saved['timeline_plan']['status'],'shortage')
        empty=self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':[]}])
        self.assertEqual(empty['timeline_plan']['status'],'shortage')
        self.assertEqual(empty['timeline_plan']['windows'],[])
        self.assertEqual(empty['timeline_plan']['shortages'][0]['segment_id'],'s1')

    def test_ranked_candidates_are_allocated_and_retained(self):
        draft=self.make_draft();ranked=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4'],'confidence':.9,'reason':'相关'}]
        with patch.object(vision,'analyze_asset',return_value={'scenes':self.scenes(),'warnings':[]}),patch.object(script_engine,'match_segments',return_value={'matches':[],'candidates':ranked,'unmatched':[]}):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed')
        stored=self.studio.store.draft(draft['id'])
        self.assertEqual(stored['match_candidates'],ranked)
        self.assertEqual(stored['timeline_plan']['status'],'ready')
    def test_legacy_candidates_survive_actual_plan_reallocation(self):
        draft=self.ready_draft()
        self.studio._apply_timeline(draft,self.studio.settings.public(),voice_seconds=[28],phase='actual')
        self.assertEqual(draft['match_candidates'][0]['clip_ids'],['c0','c1','c2','c3','c4'])
        self.assertEqual(draft['timeline_plan']['status'],'ready')
        self.studio._timeline_inputs(draft)
    def test_stale_ready_plan_is_recalculated_not_trusted_before_fish(self):
        draft=self.ready_draft();draft['matches'][0]['clip_ids']=['c0'];draft['timeline_plan']={'status':'ready'}
        with patch.object(providers,'_post') as paid,self.assertRaisesRegex(ValueError,'自动排片'):
            self.studio.bound_v2(draft)
        paid.assert_not_called()
    def test_paid_confirmation_still_required(self):
        draft=self.ready_draft();count=len(self.studio.store.jobs())
        with patch.object(providers,'_post') as paid,self.assertRaises(ValueError):
            self.studio.render(draft['id'],False)
        paid.assert_not_called();self.assertEqual(len(self.studio.store.jobs()),count)
    def test_voice_key_unchanged_by_draft_revision_and_clip_choice(self):
        first=self.studio._voice_key('同一句口播','voice-id')
        draft=self.ready_draft();self.studio.replace_matches_v2(draft['id'],[{'segment_id':'s1','clip_ids':['c1','c2','c3','c4']}])
        self.assertEqual(first,self.studio._voice_key('同一句口播','voice-id'))
        self.assertNotEqual(first,self.studio._voice_key('改一句口播','voice-id'))

if __name__=='__main__':unittest.main()

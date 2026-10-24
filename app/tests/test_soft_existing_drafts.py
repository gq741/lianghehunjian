"""Old estimated plans migrate only in the local response, without model calls."""
import copy
import unittest
from unittest.mock import patch

from mixstudio import providers
import test_v2 as fixtures


class SoftExistingDraftTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    tearDown = fixtures.V2Tests.tearDown
    wait = fixtures.V2Tests.wait
    generated = fixtures.V2Tests.generated
    make_draft = fixtures.V2Tests.make_draft
    scenes = fixtures.V2Tests.scenes

    def legacy(self):
        draft = self.make_draft()
        text = '看' * 40
        draft.update(voiceover=text, segments=[{'segment_id':'s1','text':text,'visual_hint':'外观'}],
            clips=self.scenes(), timing_status='needs_adjustment', match_status='needs_review',
            matches=[], match_candidates=[{'segment_id':'s1','clip_ids':['c0','c1','c2','c3','c4']}],
            timeline_plan={'version':1,'status':'shortage','phase':'estimated','speed':1.,
                           'segments':[],'windows':[],'shortages':[],'warnings':['old hard target']})
        self.studio.store.put_draft(draft)
        return draft

    def public(self, identifier):
        return next(d for d in self.studio.state()['drafts'] if d['id'] == identifier)

    def test_old_review_plan_projects_ready_locally_once_without_store_or_api(self):
        draft = self.legacy(); before = copy.deepcopy(draft)
        with patch.object(providers, '_post', side_effect=AssertionError('No paid calls')) as paid, \
             patch.object(self.studio.store, 'put_draft', side_effect=AssertionError('No DB writes')) as write, \
             patch.object(self.studio, '_apply_timeline', wraps=self.studio._apply_timeline) as plan:
            first = self.public(draft['id'])
            second = self.public(draft['id'])
        self.assertEqual(first['match_status'], 'ready')
        self.assertEqual(first['timeline_plan']['policy'], 'flexible_reference_v1')
        self.assertEqual(first['timeline_plan'], second['timeline_plan'])
        self.assertTrue(first['matches'])
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(self.studio.store.draft(draft['id']), before)
        paid.assert_not_called(); write.assert_not_called()

    def test_response_cache_returns_detached_projection(self):
        draft = self.legacy()
        first = self.public(draft['id'])
        self.assertEqual(first['match_status'], 'ready')
        first['timeline_plan']['status'] = 'tampered'
        first['matches'].clear()
        second = self.public(draft['id'])
        self.assertEqual(second['timeline_plan']['status'], 'ready')
        self.assertTrue(second['matches'])

    def test_backend_old_review_plan_replans_without_mutating_draft(self):
        draft = self.legacy(); before = copy.deepcopy(draft)
        with patch.object(providers, '_post', side_effect=AssertionError('No paid calls')) as paid:
            bound = self.studio.bound_v2(draft)
        self.assertEqual(bound[0]['text'], draft['voiceover'])
        self.assertTrue(bound[0]['matched_clip_ids'])
        self.assertEqual(draft, before)
        self.assertEqual(self.studio.store.draft(draft['id']), before)
        paid.assert_not_called()

    def test_active_task_defers_projection_and_backend_replan(self):
        draft = self.legacy()
        self.studio.store.create_job('active-matching', 'match', {'draft_id':draft['id']})
        self.studio.store.update_job('active-matching', status='running')
        with patch.object(self.studio, '_apply_timeline') as plan:
            self.assertEqual(self.public(draft['id'])['match_status'], 'needs_review')
            with self.assertRaises(ValueError): self.studio.bound_v2(draft)
        plan.assert_not_called()

    def test_new_policy_shortage_and_empty_candidates_stay_blocked(self):
        draft = self.legacy()
        for changes in ({'timeline_plan':{**draft['timeline_plan'],'policy':'flexible_reference_v1'}},
                        {'match_candidates':[]}, {'match_candidates':[{'segment_id':'s1','clip_ids':[]}]}):
            with self.subTest(changes=changes):
                current = {**draft, **changes}
                self.studio.store.put_draft(current)
                with patch.object(self.studio, '_apply_timeline') as plan:
                    self.assertEqual(self.public(draft['id'])['match_status'], 'needs_review')
                    with self.assertRaises(ValueError): self.studio.bound_v2(current)
                plan.assert_not_called()

    def test_registry_change_invalidates_cached_projection_and_preserves_source_checks(self):
        draft = self.legacy()
        self.assertEqual(self.public(draft['id'])['match_status'], 'ready')
        changed = {**self.asset, 'path':str(self.root/'different.mp4')}
        self.studio.store.put_asset(changed)
        self.assertEqual(self.public(draft['id'])['match_status'], 'needs_review')
        with self.assertRaisesRegex(ValueError, '路径'):
            self.studio.bound_v2(draft)

    def test_existing_real_shortage_not_bypassed_by_legacy_policy(self):
        draft = self.legacy()
        draft.update(voiceover=fixtures.TIMED_TEXT,
                     segments=[{'segment_id':'s1','text':fixtures.TIMED_TEXT,'visual_hint':'外观'}],
                     match_candidates=[{'segment_id':'s1','clip_ids':['c0']}])
        self.studio.store.put_draft(draft)
        self.assertEqual(self.public(draft['id'])['match_status'], 'needs_review')
        with self.assertRaises(ValueError): self.studio.bound_v2(draft)

    def test_old_matches_fallback_is_allowed_but_paid_confirmation_remains(self):
        draft = self.legacy()
        draft['matches'] = draft.pop('match_candidates')
        self.studio.store.put_draft(draft)
        self.assertEqual(self.public(draft['id'])['match_status'], 'ready')
        count = len(self.studio.store.jobs())
        with patch.object(providers, '_post') as paid, self.assertRaises(ValueError):
            self.studio.render(draft['id'], False)
        self.assertEqual(len(self.studio.store.jobs()), count)
        paid.assert_not_called()

    def test_invalid_legacy_reference_does_not_break_polling_or_enable_render(self):
        draft = self.legacy()
        draft['target_seconds'] = '25'
        self.studio.store.put_draft(draft)
        public = self.public(draft['id'])
        self.assertEqual(public['timing_status'], 'needs_adjustment')
        self.assertEqual(public['match_status'], 'needs_review')
        self.assertEqual(self.studio.store.draft(draft['id']), draft)


if __name__ == '__main__': unittest.main()

"""Paid-provider mocks; actual isolated job persistence, PCM cache and render."""
from pathlib import Path
import unittest
from unittest.mock import patch

import test_whole_voice as fixture
from mixstudio import providers, script_engine
from mixstudio.v2 import reusable_matching


class MatchingStateTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionTests.tearDownClass.__func__)
    setUp = fixture.ProductionTests.setUp
    tearDown = fixture.ProductionTests.tearDown
    wait = fixture.ProductionTests.wait
    voice = fixture.ProductionTests.voice
    timing = fixture.ProductionTests.timing
    scenes = fixture.ProductionTests.scenes
    matching = fixture.ProductionTests.matching
    patches = fixture.ProductionTests.patches

    def test_free_realign_after_semantic_failure_does_not_certify_empty_pool(self):
        self.studio.edit_v2('d', fixture.TEXT)
        with self.patches(), patch.object(script_engine, 'match_segments',
                side_effect=providers.ProviderError('mock failure', code='invalid_response')):
            failed = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(failed['status'], 'failed')
        saved = self.studio.store.draft('d')
        self.assertTrue(saved['analysis_summary']['complete'])
        self.assertTrue(Path(saved['whole_audio']['path']).is_file())
        self.assertEqual(saved['match_status'], 'pending')
        self.calls.clear()
        with self.patches():
            aligned = self.wait(self.studio.realign_whole('d'))
        self.assertEqual(aligned['status'], 'completed', aligned)
        self.assertEqual([n for n,_ in self.calls], ['alignment'])
        self.assertEqual(self.studio.store.draft('d')['match_status'], 'pending')
        self.calls.clear()
        with self.patches():
            completed = self.wait(self.studio.retry(failed['id'], True))
        self.assertEqual(completed['status'], 'completed', completed)
        self.assertTrue(Path(completed['result']['path']).is_file())
        self.assertIn('deepseek', [n for n,_ in self.calls])
        self.assertNotIn('fish', [n for n,_ in self.calls])

    def partial(self):
        self.studio.store.put_draft({**self.draft, 'clips':self.scenes()['scenes'],
            'analysis_summary':{'planned':2,'completed':1,'pending':1,'complete':False}})
        self.calls.clear()

    def test_manual_pool_on_partial_vision_is_reused_for_budget_and_render(self):
        self.partial()
        chosen=[{'segment_id':s['segment_id'], 'clip_ids':['c']} for s in fixture.SEGMENTS]
        self.studio.replace_matches_v2('d', chosen)
        budget=self.studio.matching_budget('d', 'produce')
        self.assertEqual(budget.get('new_request_count'),0,budget)
        with self.patches(), patch.object(self.studio,'_match_operation',
                side_effect=AssertionError('manual pool was overwritten')):
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'completed',job)
        self.assertTrue(Path(job['result']['path']).is_file())
        self.assertFalse({'glm','deepseek'}.intersection(n for n,_ in self.calls))

    def test_explicit_empty_partial_selection_stays_empty_until_edited(self):
        self.partial()
        saved=self.studio.replace_matches_v2('d',[])
        self.assertTrue(reusable_matching(saved))
        with self.patches(), patch.object(self.studio,'_match_operation',
                side_effect=AssertionError('manual pool was overwritten')) as matching:
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'failed')
        self.assertIn('缺',job['error'])
        matching.assert_not_called()
        edited=self.studio.edit_v2('d',fixture.TEXT)
        self.assertFalse(reusable_matching(edited))

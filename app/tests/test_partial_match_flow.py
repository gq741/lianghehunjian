"""Offline orchestration regressions: partial vision is useful, not fabricated."""
import copy
import unittest
from unittest.mock import patch

import test_v2 as fixtures
from mixstudio import providers, script_engine, vision
from mixstudio.service import fingerprint


class PartialMatchFlowTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    tearDown = fixtures.V2Tests.tearDown
    wait = fixtures.V2Tests.wait
    generated = fixtures.V2Tests.generated
    make_draft = fixtures.V2Tests.make_draft
    scenes = fixtures.V2Tests.scenes

    def partial(self, scenes=None, pause=False):
        scenes = self.scenes() if scenes is None else scenes
        return {'scenes': scenes, 'warnings': ['一批待恢复，已有结果保留'],
                'analysis_summary': {'planned': 10, 'completed': len(scenes),
                    'pending': 10-len(scenes), 'complete': False,
                    'pause_new_requests': pause, 'stop_reason': 'http_503' if pause else '',
                    'failed_batches': [{'batch_index': 4, 'scene_count': 2,
                        'clip_ids': ['pending1','pending2'], 'code': 'http_503',
                        'diagnostic': {'http_status': 503}}]}}

    def match(self):
        return {'candidates': [{'segment_id': 's1', 'clip_ids': [f'c{i}' for i in range(5)],
                               'confidence': .95, 'reason': '同商品外观'}], 'unmatched': []}

    def test_partial_analysis_automatically_matches_and_is_render_ready(self):
        draft = self.make_draft()
        with patch.object(vision, 'analyze_asset', return_value=self.partial()) as analyze, \
             patch.object(script_engine, 'match_segments', return_value=self.match()) as match:
            job = self.wait(self.studio.match_v2(draft['id'], True))
        self.assertEqual(job['status'], 'completed', job)
        saved = self.studio.store.draft(draft['id'])
        self.assertEqual(saved['analysis_summary']['completed'], 6)
        self.assertEqual(saved['analysis_summary']['pending'], 4)
        self.assertFalse(saved['analysis_summary']['complete'])
        self.assertEqual(saved['match_status'], 'ready')
        self.assertEqual(match.call_count, 1)
        self.assertTrue(analyze.call_args.kwargs['allow_partial'])
        self.assertEqual(job['result']['analysis_summary'], saved['analysis_summary'])

    def test_provider_pause_reads_later_asset_cache_without_more_requests(self):
        draft = self.make_draft()
        source = self.root/'second.mp4'; source.write_bytes(b'second fixture')
        asset = {**self.asset, 'id':'a2', 'path':str(source), 'sha256':fingerprint(source)}
        # Orchestration-only fixture: the fake second file has its own probe
        # identity, not the descriptor of the first source copied above.
        from mixstudio.preprocess import PROBE_VERSION
        from mixstudio.media_identity import file_version
        asset['prepared_media'] = {**self.asset['prepared_media'],
            'source_sha256':asset['sha256'], 'probe_version':PROBE_VERSION,
            'source_version':list(file_version(source))}
        self.studio.store.put_asset(asset)
        draft['asset_ids'].append('a2');self.studio.store.put_draft(draft)
        calls=[]
        def analyze(asset, *unused, **kw):
            calls.append(kw)
            return self.partial(pause=True) if asset['id']=='a1' else {
                'scenes': [], 'warnings': [], 'analysis_summary': {
                    'planned':10,'completed':0,'pending':10,'complete':False,
                    'failed_batches':[],'pause_new_requests':False,'stop_reason':'cache_only'}}
        with patch.object(vision,'analyze_asset',side_effect=analyze), \
             patch.object(script_engine,'match_segments',return_value=self.match()):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        self.assertTrue(calls[1]['cache_only'])
        summary=self.studio.store.draft(draft['id'])['analysis_summary']
        self.assertEqual((summary['completed'],summary['planned'],summary['pending']),(6,20,14))

    def test_partial_without_usable_frames_saves_progress_without_semantic_call(self):
        draft=self.make_draft()
        scenes=[{**s,'usable':False} for s in self.scenes()]
        with patch.object(vision,'analyze_asset',return_value=self.partial(scenes)), \
             patch.object(script_engine,'match_segments',side_effect=AssertionError('No usable input')) as match:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        saved=self.studio.store.draft(draft['id'])
        self.assertEqual(saved['analysis_summary']['completed'],6)
        self.assertEqual(saved['match_status'],'needs_review')
        self.assertEqual(match.call_count,0)
        self.assertEqual(saved['clips'],scenes)

    def test_semantic_failure_keeps_vision_checkpoint_in_draft(self):
        draft=self.make_draft()
        with patch.object(vision,'analyze_asset',return_value=self.partial()), \
             patch.object(script_engine,'match_segments',side_effect=providers.ProviderError('offline',code='http_503',result_unknown=True)):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed')
        saved=self.studio.store.draft(draft['id'])
        self.assertEqual(saved['clips'],self.scenes())
        self.assertEqual(saved['analysis_summary']['completed'],6)
        self.assertEqual(saved['match_status'],'pending')

    def test_existing_ready_plan_survives_later_semantic_failure(self):
        draft=self.make_draft()
        with patch.object(vision,'analyze_asset',return_value=self.partial()), \
             patch.object(script_engine,'match_segments',return_value=self.match()):
            self.assertEqual(self.wait(self.studio.match_v2(draft['id'],True))['status'],'completed')
        before=copy.deepcopy(self.studio.store.draft(draft['id']))
        with patch.object(vision,'analyze_asset',return_value=self.partial()), \
             patch.object(script_engine,'match_segments',side_effect=providers.ProviderError('offline',code='http_503')):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed')
        after=self.studio.store.draft(draft['id'])
        self.assertIn('analysis_summary',after)
        self.assertEqual(after['match_status'],'ready')
        self.assertEqual(after['timeline_plan'],before['timeline_plan'])
        self.assertEqual(after['match_candidates'],before['match_candidates'])

    def test_fatal_source_error_does_not_call_semantic_match(self):
        draft=self.make_draft()
        with patch.object(vision,'analyze_asset',side_effect=providers.ProviderError('source changed',code='source_changed')), \
             patch.object(script_engine,'match_segments') as match:
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed')
        self.assertEqual(match.call_count,0)

    def test_legacy_none_candidates_does_not_drop_checkpoint(self):
        draft=self.make_draft()
        draft['match_candidates']=None
        self.studio.store.put_draft(draft)
        with patch.object(vision,'analyze_asset',return_value=self.partial()), \
             patch.object(script_engine,'match_segments',return_value=self.match()):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'completed',job)
        saved=self.studio.store.draft(draft['id'])
        self.assertEqual(saved['analysis_summary']['completed'],6)
        self.assertEqual(saved['match_status'],'ready')

    def test_newly_unusable_scene_invalidates_old_ready_plan(self):
        draft=self.make_draft()
        with patch.object(vision,'analyze_asset',return_value=self.partial()), \
             patch.object(script_engine,'match_segments',return_value=self.match()):
            self.wait(self.studio.match_v2(draft['id'],True))
        scenes=self.scenes();scenes[0]['usable']=False
        with patch.object(vision,'analyze_asset',return_value=self.partial(scenes)), \
             patch.object(script_engine,'match_segments',side_effect=providers.ProviderError('offline',code='http_503')):
            job=self.wait(self.studio.match_v2(draft['id'],True))
        self.assertEqual(job['status'],'failed')
        saved=self.studio.store.draft(draft['id'])
        self.assertEqual(saved['match_status'],'pending')
        self.assertIsNone(saved['timeline_plan'])
        self.assertEqual(saved['match_candidates'],[])
        self.assertEqual(saved['analysis_summary']['completed'],6)


if __name__=='__main__': unittest.main()

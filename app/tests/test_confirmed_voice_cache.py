"""Offline regressions for paid semantic commit and timeline invalidation."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine as engine, semantic_cache, vision
from mixstudio.settings import DEFAULTS
from mixstudio.v2 import V2Actions
from mixstudio.whole_voice import WholeVoiceActions


SCENE = {'clip_id': 'new_c', 'asset_id': 'a', 'source_path': 'fixture.mp4',
         'start': 0., 'end': 3., 'text': '商品外观', 'tags': ['外观'],
         'usable': True, 'reason': '同商品', 'confidence': .95}


def matching():
    return {'candidates': [{'segment_id': 's1', 'clip_ids': ['new_c'],
                           'confidence': .95, 'reason': '外观一致'}], 'unmatched': []}


def wire(result):
    return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps(result, ensure_ascii=False)}}]}, ensure_ascii=False).encode()


class SemanticCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'semantic-cache'
        self.event = threading.Event()
        self.segments = engine.split_segments('看看商品外观。')
        self.no_network = patch.object(providers, '_http_open',
                                       side_effect=AssertionError('Network forbidden'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def match(self):
        return engine.match_segments(self.segments, [SCENE], api_key='offline-placeholder',
            model=DEFAULTS['deepseek_model'], product_title='测试商品',
            cancel=self.event, cache_dir=self.cache)

    def test_completed_valid_response_is_cached_before_cancel_and_retry_reuses_it(self):
        def completed(*args, **kwargs):
            self.event.set()
            return wire(matching()), 'application/json'
        with patch.object(providers, '_post', side_effect=completed) as post:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
            self.assertEqual(caught.exception.code, 'cancelled')
            self.assertFalse(caught.exception.result_unknown)
            self.assertEqual(len(list(self.cache.glob('*.json'))), 1)
            self.assertTrue(post.call_args.kwargs['commit_complete'])
            self.event.clear()
            self.assertEqual(len(self.match()['candidates']), 1)
            self.assertEqual(post.call_count, 1)

    def test_real_transport_final_read_commits_then_cancels(self):
        body = wire(matching())
        event = self.event
        class Response:
            headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                event.set()
                return body
        with patch.object(providers, '_http_open', return_value=Response()) as network:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
            self.assertEqual(caught.exception.code, 'cancelled')
            self.assertFalse(caught.exception.result_unknown)
            self.assertEqual(len(list(self.cache.glob('*.json'))), 1)
            self.event.clear()
            self.assertEqual(len(self.match()['candidates']), 1)
            self.assertEqual(network.call_count, 1)

    def test_incomplete_transport_cancel_does_not_save_success(self):
        body = wire(matching())
        event = self.event
        class Response:
            headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                event.set()
                return body[:10]
        with patch.object(providers, '_http_open', return_value=Response()) as network:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertTrue(caught.exception.result_unknown)
        self.assertEqual(list(self.cache.glob('*.json')), [])
        self.assertEqual(network.call_count, 1)

    def test_complete_response_still_requires_full_semantic_validation(self):
        invalid = matching()
        invalid['candidates'][0]['clip_ids'] = ['not_in_this_batch']
        with patch.object(providers, '_post', return_value=(wire(invalid), 'application/json')) as post:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
        self.assertEqual(caught.exception.code, 'invalid_response')
        self.assertEqual(list(self.cache.glob('*.json')), [])
        self.assertEqual(post.call_count, 1)

    def test_cancel_before_submit_never_calls_provider(self):
        self.event.set()
        with patch.object(providers, '_post') as post:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
        self.assertEqual(caught.exception.code, 'cancelled')
        post.assert_not_called()

    def test_cancelled_complete_response_survives_checkpoint_disk_failure(self):
        failure = patch.object(semantic_cache, '_atomic_write', side_effect=PermissionError('fixture'))
        self.addCleanup(failure.stop)
        def completed(*args, **kwargs):
            # Cache preflight already passed; emulate storage failing only
            # after the provider has produced the complete response.
            failure.start()
            self.event.set()
            return wire(matching()), 'application/json'
        with patch.object(providers, '_post', side_effect=completed) as post:
            with self.assertRaises(providers.ProviderError) as caught:
                self.match()
            self.assertEqual(caught.exception.code, 'cache_write')
            self.assertFalse(caught.exception.result_unknown)
            self.assertEqual(list(self.cache.glob('*.json')), [])
            failure.stop()
            self.event.clear()
            self.assertEqual(len(self.match()['candidates']), 1)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(list(self.cache.glob('*.json'))), 1)


class MemoryStore:
    def __init__(self, draft):
        self.value = copy.deepcopy(draft)
        self.on_write = None
    def draft(self, identifier): return copy.deepcopy(self.value)
    def active_jobs(self): return []
    def put_draft(self, value):
        self.value = copy.deepcopy(value)
        if self.on_write: self.on_write(value)


class SyncJobs:
    def __init__(self, event): self.event = event
    def submit(self, kind, payload, work, **kwargs):
        return work(self.event, lambda *args: None)


class Fixture(WholeVoiceActions, V2Actions):
    def __init__(self, root, draft, event):
        self.root = root
        self.action_lock = threading.RLock()
        self.asr_slot = threading.Semaphore(1)
        self.store = MemoryStore(draft)
        self.jobs = SyncJobs(event)
        self.settings = SimpleNamespace(public=lambda: copy.deepcopy(DEFAULTS),
                                        secret=lambda name: 'offline-placeholder')
    def _v2_assets(self, identifiers):
        return [{'id': 'a', 'duration': 3., 'sha256': 'a' * 64,
                 'path': str(self.root / 'fixture.mp4')}]
    def _prepare_assets(self, identifiers, event, progress): return self._v2_assets(identifiers)
    def _unchanged(self, *args): pass
    def _free_preflight(self, *args, **kwargs): pass


def old_draft():
    text = '看看商品外观。'
    return {'id': 'd', 'workflow': 'script_first_v2', 'production_mode': 'whole_voice_v1',
            'voiceover': text, 'segments': engine.split_segments(text), 'asset_ids': ['a'],
            'target_seconds': 25, 'revision': 1, 'product_name': '测试商品',
            'clips': [], 'matches': [], 'match_status': 'ready',
            'whole_voice': {'duration': 8.},
            'timeline_plan': {'status': 'ready', 'total_seconds': 8.},
            'whole_timeline_plan': {'status': 'ready', 'total_seconds': 8.},
            'match_candidates': [{'segment_id': 's1', 'clip_ids': ['old_c']}]}


class TimelineInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.event = threading.Event()
        self.studio = Fixture(Path(self.temp.name), old_draft(), self.event)
        no_network = patch.object(providers, '_http_open',
                                  side_effect=AssertionError('Network forbidden'))
        no_network.start()
        self.addCleanup(no_network.stop)

    def test_new_scene_pool_invalidates_whole_plan_before_semantic_failure(self):
        with patch.object(vision, 'analyze_asset', return_value={'scenes': [SCENE]}), \
                patch.object(engine, 'match_segments', side_effect=providers.ProviderError(
                    'mock semantic failure', code='invalid_response')):
            with self.assertRaises(providers.ProviderError):
                self.studio.match_v2('d', True)
        current = self.studio.store.value
        self.assertEqual(current['match_candidates'], [])
        self.assertIsNone(current.get('timeline_plan'))
        self.assertIsNone(current.get('whole_timeline_plan'))
        self.assertEqual(current['match_status'], 'pending')
        self.assertEqual(current['clips'][0]['clip_id'], 'new_c')

    def test_new_audio_timing_invalidates_both_plans_before_cancel(self):
        from mixstudio.jobs import Cancelled
        self.studio.store.on_write = lambda current: self.event.set() if (
            current.get('whole_voice', {}).get('duration') == 10.) else None
        timing = {'segment_seconds': [10.], 'captions': [], 'method': 'fixture', 'warnings': []}
        with patch.object(self.studio, '_whole_audio', return_value=(
                self.studio.root / 'fixture.wav', 'key', 'signature', 10.)), \
                patch.object(self.studio, '_align_whole', return_value=timing):
            with self.assertRaises(Cancelled):
                self.studio.produce_whole('d', True)
        current = self.studio.store.value
        self.assertEqual(current['whole_audio']['duration'], 10.)
        self.assertEqual(current['whole_voice']['duration'], 10.)
        self.assertIsNone(current.get('timeline_plan'))
        self.assertIsNone(current.get('whole_timeline_plan'))
        self.assertEqual(current['match_status'], 'pending')
        self.assertEqual(current['match_candidates'][0]['clip_ids'], ['old_c'])


if __name__ == '__main__':
    unittest.main()

"""Offline regressions for paid submission and local checkpoint integrity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine, semantic_cache
from mixstudio.v2 import V2Actions

PRODUCT = {'sku': '100012345678', 'title': '测试品牌电视 55英寸',
           'source': 'https://item.jd.com/100012345678.html', 'observed_at': 123456, 'status': 'read'}


class GenerationDedupTests(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.model = 'deepseek-chat'
        self.product = dict(PRODUCT)
        owner = self
        class Actions(V2Actions):
            action_lock = threading.Lock()
            settings = SimpleNamespace(public=lambda: {'deepseek_model': owner.model}, secret=lambda _: 'offline-key')
            store = SimpleNamespace(meta=lambda _: owner.product,
                                    active_jobs=lambda: [j for j in owner.jobs if j['status'] in {'queued', 'running', 'cancelling'}])
            def _v2_assets(self, ids):
                return [{'id': i, 'sha256': 'a'*64 if i in {'a', 'alias'} else 'b'*64} for i in ids]
        self.app = Actions()
        def submit(kind, payload, work):
            job = {'id': str(len(self.jobs)), 'kind': kind, 'payload': copy.deepcopy(payload), 'status': 'queued'}
            self.jobs.append(job)
            return job
        self.app.jobs = SimpleNamespace(submit=submit)
        self.payload = {'asset_ids': ['a'], 'target_seconds': 25, 'count': 1, 'allow_paid': True, 'voice_first': True}

    def test_identical_concurrent_active_requests_return_one_job(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            jobs = list(pool.map(lambda _: self.app.generate_v2(self.payload), range(24)))
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual({j['id'] for j in jobs}, {'0'})

    def test_aliases_and_asset_order_do_not_duplicate_content(self):
        first = self.app.generate_v2({**self.payload, 'asset_ids': ['a', 'b']})
        second = self.app.generate_v2({**self.payload, 'asset_ids': ['b', 'alias']})
        self.assertEqual(first['id'], second['id'])

    def test_completed_request_allows_intentional_regeneration(self):
        self.app.generate_v2(self.payload)
        self.jobs[0]['status'] = 'completed'
        self.app.generate_v2(self.payload)
        self.assertEqual(len(self.jobs), 2)

    def test_distinct_model_pipeline_title_and_parameters_stay_distinct(self):
        self.app.generate_v2(self.payload)
        for changes in ({'count': 2}, {'target_seconds': 26}, {'voice_first': False}, {'asset_ids': ['b']}):
            self.app.generate_v2({**self.payload, **changes})
        self.model = 'deepseek-other'
        self.app.generate_v2(self.payload)
        self.product = {**PRODUCT, 'title': '测试品牌电视 65英寸'}
        self.app.generate_v2(self.payload)
        self.assertEqual(len(self.jobs), 7)


class CheckpointIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)/'semantic-cache'
        self.segments = [{'segment_id': 's1', 'text': '电视正面。', 'visual_hint': '正面'}]
        self.scenes = [{'clip_id': 'c1', 'usable': True, 'confidence': .9, 'start': 0, 'end': 3,
                        'text': '电视正面', 'tags': ['外观'], 'ocr': '', 'source_path': 'unused.mp4', 'asset_id': 'asset'}]
        self.calls = 0
        self.result = {'candidates': [{'segment_id': 's1', 'clip_ids': ['c1'], 'confidence': .9, 'reason': '正面'}], 'unmatched': []}

    def complete(self, *args, **kwargs):
        self.calls += 1
        return copy.deepcopy(self.result)

    def match(self):
        return script_engine.match_segments(self.segments, self.scenes, api_key='offline-key',
            model='deepseek-chat', product_title='电视', cache_dir=self.cache, cache_context={'fixture': self.tmp.name})

    def test_primary_failure_recovers_from_independent_checkpoint_without_rebilling(self):
        replace = semantic_cache.os.replace
        def fail_primary(source, destination):
            if Path(destination).parent == self.cache:
                raise OSError('primary fixture failure')
            return replace(source, destination)
        with patch.object(script_engine, '_complete', side_effect=self.complete), patch.object(semantic_cache.os, 'replace', side_effect=fail_primary):
            first = self.match()
        with patch.object(script_engine, '_complete', side_effect=AssertionError('must reuse paid result')):
            self.assertEqual(first, self.match())
        self.assertEqual(self.calls, 1)
        self.assertTrue(list(Path(self.tmp.name).rglob('semantic-*.json')))

    def test_primary_directory_creation_failure_still_uses_recovery(self):
        mkdir = Path.mkdir
        def fail_primary(path, *args, **kwargs):
            if path == self.cache:
                raise PermissionError('primary directory blocked')
            return mkdir(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', new=fail_primary), patch.object(script_engine, '_complete', side_effect=self.complete):
            first = self.match()
        with patch.object(script_engine, '_complete', side_effect=AssertionError('must reuse recovery')):
            self.assertEqual(first, self.match())
        self.assertEqual(self.calls, 1)

    def test_both_destinations_unwritable_block_before_paid_call(self):
        with patch.object(semantic_cache.os, 'replace', side_effect=OSError('both blocked')):
            with patch.object(script_engine, '_complete') as api, self.assertRaises(providers.ProviderError) as caught:
                self.match()
        api.assert_not_called()
        self.assertEqual(caught.exception.code, 'cache_write')
        self.assertFalse(caught.exception.result_unknown)

    def test_disk_failure_after_paid_result_preserves_memory_and_is_not_unknown(self):
        def result_then_fail(*args, **kwargs):
            self.calls += 1
            failing.start()
            return copy.deepcopy(self.result)
        failing = patch.object(semantic_cache.os, 'replace', side_effect=OSError('disk became full'))
        try:
            with patch.object(script_engine, '_complete', side_effect=result_then_fail):
                with self.assertRaises(providers.ProviderError) as caught:
                    self.match()
            self.assertEqual(caught.exception.code, 'cache_write')
            self.assertFalse(caught.exception.result_unknown)
            self.assertIn('内存', str(caught.exception))
            with patch.object(script_engine, '_complete') as api, self.assertRaises(providers.ProviderError) as retry_error:
                self.match()
            api.assert_not_called()
            self.assertFalse(retry_error.exception.result_unknown)
            failing.stop()
            with patch.object(script_engine, '_complete', side_effect=AssertionError('must reuse in-memory result')):
                out = self.match()
            self.assertTrue(list(self.cache.glob('semantic-*.json')))
            self.assertEqual(out['candidates'][0]['clip_ids'], ['c1'])
            self.assertEqual(self.calls, 1)
        finally:
            failing.stop()

    def test_budget_reads_undurable_memory_without_writing_or_paid_calls(self):
        def result_then_fail(*args, **kwargs):
            failing.start()
            return self.complete(*args, **kwargs)
        failing = patch.object(semantic_cache.os, 'replace', side_effect=OSError('disk became full'))
        try:
            with patch.object(script_engine, '_complete', side_effect=result_then_fail), self.assertRaises(providers.ProviderError):
                self.match()
            before = sorted(str(p) for p in Path(self.tmp.name).rglob('*'))
            with patch.object(semantic_cache, '_atomic_write', side_effect=AssertionError('budget must not write')), patch.object(script_engine, '_complete') as api:
                budget = script_engine.match_budget(self.segments, self.scenes, model='deepseek-chat',
                    product_title='电视', cache_dir=self.cache, cache_context={'fixture': self.tmp.name})
            api.assert_not_called()
            self.assertEqual(budget['new_request_count'], 0)
            self.assertEqual(budget['cached_request_count'], 1)
            self.assertEqual(before, sorted(str(p) for p in Path(self.tmp.name).rglob('*')))
            self.assertEqual(self.calls, 1)
        finally:
            failing.stop()

    def test_relative_cache_budget_uses_same_recovery_identity(self):
        replace = semantic_cache.os.replace
        def fail_primary(source, destination):
            if Path(destination).parent == self.cache:
                raise OSError('primary fixture failure')
            return replace(source, destination)
        with patch.object(script_engine, '_complete', side_effect=self.complete), patch.object(semantic_cache.os, 'replace', side_effect=fail_primary):
            self.match()
        with chdir(self.tmp.name), patch.object(script_engine, '_complete') as api:
            budget = script_engine.match_budget(self.segments, self.scenes, model='deepseek-chat',
                product_title='电视', cache_dir=Path('semantic-cache'), cache_context={'fixture': self.tmp.name})
        api.assert_not_called()
        self.assertEqual(budget['new_request_count'], 0)
        self.assertEqual(self.calls, 1)

    def test_legacy_cache_stays_compatible_and_corruption_blocks_rebilling(self):
        with patch.object(script_engine, '_complete', side_effect=self.complete):
            self.match()
        path = next(self.cache.glob('semantic-*.json'))
        envelope = json.loads(path.read_text('utf8'))
        self.assertEqual(envelope['version'], 1)
        with patch.object(script_engine, '_complete', side_effect=AssertionError('legacy must be reused')):
            self.match()
        path.write_text('{broken', encoding='utf8')
        with patch.object(script_engine, '_complete') as api, self.assertRaises(providers.ProviderError) as caught:
            self.match()
        api.assert_not_called()
        self.assertEqual(caught.exception.code, 'invalid_cache')

    def test_corrupt_recovery_checkpoint_is_not_a_cache_miss(self):
        replace = semantic_cache.os.replace
        def fail_primary(source, destination):
            if Path(destination).parent == self.cache:
                raise OSError('primary fixture failure')
            return replace(source, destination)
        with patch.object(script_engine, '_complete', side_effect=self.complete), patch.object(semantic_cache.os, 'replace', side_effect=fail_primary):
            self.match()
        path = next(Path(self.tmp.name).rglob('semantic-*.json'))
        path.write_text('{broken', encoding='utf8')
        with patch.object(script_engine, '_complete') as api, self.assertRaises(providers.ProviderError) as caught:
            self.match()
        api.assert_not_called()
        self.assertEqual(caught.exception.code, 'invalid_cache')


if __name__ == '__main__':
    unittest.main()

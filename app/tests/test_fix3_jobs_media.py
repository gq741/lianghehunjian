"""C02/C03/C06 regressions. Isolated SQLite/media; every cloud provider mocked."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(os.environ.get('FIX3_SOURCE', Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tests')]
from mixstudio.jobs import JobManager
from mixstudio.service import Studio, fingerprint
from mixstudio.store import Store
import test_v2_workflow as workflow


class CommittedJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fix3-job-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite')
        self.jobs = JobManager(self.store, workers=1)
        self.addCleanup(self.jobs.shutdown)

    def late_cancel(self, kind, result):
        committed, release = threading.Event(), threading.Event()
        def work(event, progress):
            self.store.put_draft({'id': 'committed-draft', 'kind': kind})
            committed.set()
            if not release.wait(5):
                raise AssertionError('test release timed out')
            return result
        job = self.jobs.submit(kind, {}, work)
        try:
            self.assertTrue(committed.wait(5))
            self.jobs.cancel(job['id'])
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.store.job(job['id'])
            if current['status'] in {'completed', 'cancelled', 'failed'}:
                return current
            time.sleep(.01)
        self.fail('job did not finish')

    def test_committed_script_late_cancel_retains_completed_result(self):
        result = {'draft_ids': ['committed-draft'], 'committed': True}
        current = self.late_cancel('script', result)
        self.assertEqual(current['status'], 'completed', current)
        self.assertEqual(current['result'], result)

    def test_committed_match_late_cancel_retains_completed_result(self):
        result = {'draft_id': 'committed-draft', 'committed': True}
        current = self.late_cancel('match', result)
        self.assertEqual(current['status'], 'completed', current)
        self.assertEqual(current['result'], result)

    def test_non_committed_and_other_kinds_still_cancel(self):
        for kind, result in [('script', {}), ('match', {'committed': 1}),
                             ('scan', {'committed': True})]:
            with self.subTest(kind=kind, result=result):
                self.assertEqual(self.late_cancel(kind, result)['status'], 'cancelled')


class PublishedRenderTests(unittest.TestCase):
    def test_published_render_survives_final_draft_db_failure(self):
        fixture = workflow.V2WorkflowTests(
            'test_script_first_real_25_second_export_uses_only_matched_windows_and_cached_voice')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        identifier = fixture.make_matched_draft()
        original_put = fixture.studio.store.put_draft
        def fail_final(draft):
            if draft.get('status') == 'rendered':
                raise sqlite3.OperationalError('injected final metadata write failure')
            return original_put(draft)
        with patch.object(fixture.studio.store, 'put_draft', side_effect=fail_final):
            result = fixture.wait(fixture.studio.render(identifier, True))
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['result']['published'])
        self.assertTrue(result['result']['warnings'])
        path = Path(result['result']['path'])
        self.assertTrue(path.is_file())
        self.assertAlmostEqual(workflow.media.probe(path)['duration'], 25, delta=.08)
        self.assertEqual(len(list((fixture.root / '测试成片').glob('*.mp4'))), 1)
        self.assertEqual(len(fixture.voice_calls), 2)
        registered = [j for j in fixture.studio.state()['jobs'] if (j.get('result') or {}).get('path')]
        self.assertEqual(len(registered), 1)
        with self.assertRaises(ValueError):
            fixture.studio.retry(result['id'], True)
        fixture.assert_originals_and_no_network()


class MediaIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fix3-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.studio = Studio(self.root / 'runtime')
        self.addCleanup(self.studio.close)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'original-source-bytes')
        self.studio.store.activate_asset({'id': 'asset', 'path': str(self.source),
                                         'sha256': fingerprint(self.source)})
        (self.studio.root / 'thumbs' / 'asset.jpg').write_bytes(b'isolated-thumb')

    def test_repeated_thumbnail_and_preview_hash_one_file_version_once(self):
        with patch('mixstudio.service.fingerprint', wraps=fingerprint) as hashed:
            for i in range(8):
                self.studio.asset_media_path('asset', thumbnail=(i % 2 == 0))
        self.assertEqual(hashed.call_count, 1)

    def test_same_size_rewrite_with_restored_mtime_invalidates_cached_identity(self):
        self.studio.asset_media_path('asset')
        before = self.source.stat()
        time.sleep(.02)
        self.source.write_bytes(b'changed-source--bytes')
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.source.stat().st_size, before.st_size)
        with self.assertRaises(ValueError):
            self.studio.asset_media_path('asset')
        self.assertFalse(self.studio.store.asset('asset')['selectable'])

    def test_atomic_same_size_replacement_with_restored_mtime_rejected(self):
        self.studio.asset_media_path('asset')
        before = self.source.stat()
        replacement = self.root / 'replacement.mp4'
        replacement.write_bytes(b'changed-source--bytes')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.source)
        with self.assertRaises(ValueError):
            self.studio.asset_media_path('asset', thumbnail=True)

    def test_concurrent_requests_single_flight_hash(self):
        start = threading.Barrier(8)
        def slow_hash(path):
            time.sleep(.04)
            return fingerprint(path)
        def read():
            start.wait(5)
            return self.studio.asset_media_path('asset')
        with patch('mixstudio.service.fingerprint', side_effect=slow_hash) as hashed:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: read(), range(8)))
        self.assertTrue(all(path == self.source.resolve() for path in results))
        self.assertEqual(hashed.call_count, 1)

    def test_unavailable_change_version_falls_back_to_full_hash(self):
        from mixstudio import media_identity
        with patch.object(media_identity, 'file_version', return_value=None):
            with patch('mixstudio.service.fingerprint', wraps=fingerprint) as hashed:
                self.studio.asset_media_path('asset')
                self.studio.asset_media_path('asset', thumbnail=True)
                self.assertEqual(hashed.call_count, 2)

    def test_cached_hash_still_compares_current_registered_sha(self):
        self.studio.asset_media_path('asset')
        self.studio.store.put_asset({**self.studio.store.asset('asset'), 'sha256': '0' * 64})
        with self.assertRaises(ValueError):
            self.studio.asset_media_path('asset')

    def test_changing_version_during_hash_is_not_cached(self):
        from mixstudio import media_identity
        with patch.object(media_identity, 'file_version', side_effect=[('old',), ('new',)]):
            with self.assertRaisesRegex(ValueError, '发生改变'):
                self.studio.asset_media_path('asset')
        with patch('mixstudio.service.fingerprint', wraps=fingerprint) as hashed:
            self.studio.asset_media_path('asset')
            self.assertEqual(hashed.call_count, 1)

    def test_lru_cache_is_bounded_and_oldest_path_revalidated(self):
        from mixstudio.media_identity import MediaIdentityCache
        cache = MediaIdentityCache(max_entries=2)
        paths = [self.root / f'bounded-{i}.bin' for i in range(3)]
        for path in paths:
            path.write_bytes(b'bounded cache fixture')
        with patch('mixstudio.service.fingerprint', wraps=fingerprint) as hashed:
            for path in paths:
                cache.signature(path, hashed)
            self.assertEqual(len(cache._entries), 2)
            cache.signature(paths[2], hashed)
            self.assertEqual(hashed.call_count, 3)
            cache.signature(paths[0], hashed)
            self.assertEqual(hashed.call_count, 4)
            self.assertEqual(len(cache._entries), 2)


if __name__ == '__main__':
    unittest.main()

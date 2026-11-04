"""Confirmed history-index and damaged music-catalog regressions; offline only."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio.store import Store
from mixstudio.music_actions import MusicActions
from mixstudio.music_library import MusicLibrary
from mixstudio.media_identity import MediaIdentityCache


class AuditHistoryIndexTests(unittest.TestCase):
    def test_recent_history_query_uses_index_after_old_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)/'state.sqlite'
            store = Store(path)
            with store.connection() as db:
                # An existing installation has this pre-music-import index.
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall():
                    if row[0].startswith('jobs_recent_group'):
                        db.execute('DROP INDEX ' + row[0])
                db.execute("CREATE INDEX jobs_recent_group ON jobs((json_extract(data,'$.kind') IN ('scan','download')), json_extract(data,'$.created_at') DESC)")
                db.executemany('INSERT INTO jobs(id,data) VALUES(?,?)', [
                    (str(i), json.dumps({'id': str(i), 'kind': ('download','render','music_import')[i % 3],
                     'status':'completed', 'created_at':i, 'result':{'sample':'x'*512}}))
                    for i in range(3000)])
                db.execute('UPDATE jobs SET data=json_set(data,\'$.status\',\'running\') WHERE id=\'0\'')
            store = Store(path)
            original = store.connection
            statements = []
            @contextmanager
            def observed():
                with original() as db:
                    db.set_trace_callback(statements.append)
                    yield db
            with patch.object(store, 'connection', observed):
                jobs = store.jobs(limit_per_group=20)
            self.assertEqual(len(jobs), 41)
            self.assertIn('0', [j['id'] for j in jobs])
            queries = [q for q in statements if q.startswith('SELECT id,data') and 'LIMIT' in q]
            self.assertEqual(len(queries), 2)
            with original() as db:
                for query in queries:
                    plans = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query)]
                    self.assertTrue(any('SEARCH jobs USING INDEX' in p for p in plans), plans)
                    self.assertFalse(any('TEMP B-TREE' in p for p in plans), plans)


class AuditMusicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = MusicLibrary(self.root)
        body = b'owned song fixture'
        signature = hashlib.sha256(body).hexdigest()
        self.song = self.library.root / (signature + '.wav')
        self.song.write_bytes(body)
        self.library._save([{'id':signature, 'sha256':signature, 'file':self.song.name,
            'name':'fixture.wav', 'size':len(body), 'duration':1., 'imported_at':1.}])
        self.actions = MusicActions()
        self.actions.music_library = self.library
        self.actions._media_identity_cache = MediaIdentityCache()

    def select(self, path):
        settings = {'bgm_path':str(path), 'bgm_gain':.15}
        self.actions.settings = SimpleNamespace(public=lambda: dict(settings))
        return settings

    def test_damaged_manifest_rejects_selected_managed_music_before_production(self):
        settings = self.select(self.song)
        self.library.manifest.write_text('{broken', 'utf8')
        state = self.actions.music_state()
        self.assertTrue(state['error'])
        with self.assertRaisesRegex(ValueError, '清单'):
            self.actions.require_music(settings)
        self.assertEqual(self.library.manifest.read_text('utf8'), '{broken')
        self.assertTrue(self.song.is_file())

    def test_missing_manifest_cannot_turn_owned_song_into_external_music(self):
        settings = self.select(self.song)
        self.library.manifest.unlink()
        with self.assertRaisesRegex(ValueError, '音乐库'):
            self.actions.require_music(settings)
        self.assertTrue(self.song.is_file())

    def test_intact_manifest_and_legacy_external_song_remain_usable(self):
        self.actions.require_music(self.select(self.song))
        external = self.root / 'legacy.wav'
        external.write_bytes(b'legacy fixture')
        self.actions.require_music(self.select(external))
        self.library.manifest.write_text('{broken', 'utf8')
        self.actions.require_music(self.select(external))
        self.actions.require_music(self.select(''))


import test_whole_voice as fixture
from mixstudio import script_engine


class AuditEditedCopyMatchingTests(unittest.TestCase):
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

    def analyzed_draft(self, *, status='pending', choices=None):
        draft = {**self.draft, 'clips':self.scenes()['scenes'],
            'analysis_summary':{'complete':True}, 'match_candidates':choices or [],
            'match_status':status}
        self.studio.store.put_draft(draft)
        self.calls.clear()

    def test_save_copy_with_completed_visual_analysis_rematches_then_exports(self):
        self.analyzed_draft(status='ready', choices=self.matching()['candidates'])
        self.studio.edit_v2('d', fixture.TEXT)
        self.calls.clear()
        with self.patches():
            result = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(sum(c[0]=='deepseek' for c in self.calls), 1)
        self.assertTrue(Path(result['result']['path']).is_file())

    def test_pending_empty_pool_recovery_budget_does_not_claim_zero_new_matching_calls(self):
        self.analyzed_draft()
        with patch.object(script_engine, 'match_budget', return_value={'new_request_count':1}) as budget:
            result = self.studio.matching_budget('d','produce')
        self.assertEqual(budget.call_count, 1)
        self.assertEqual(result['new_request_count'], 1)

    def test_deliberately_empty_completed_choices_are_not_silently_refilled(self):
        self.analyzed_draft(status='needs_review')
        with self.patches(), patch.object(self.studio, '_match_operation', side_effect=AssertionError('manual choices must remain empty')) as matching:
            result = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(result['status'],'failed')
        matching.assert_not_called()
        self.assertIn('不足',result['error'])

    def test_cancelled_timing_refresh_keeps_deliberately_empty_choices_on_retry(self):
        self.analyzed_draft(status='needs_review')
        def timing_then_cancel(*args):
            result = self.timing(*args)
            args[-1].set()
            return result
        with self.patches(), patch.object(self.studio, '_align_whole', side_effect=timing_then_cancel):
            first = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(first['status'], 'cancelled')
        with self.patches(), patch.object(self.studio, '_match_operation', side_effect=AssertionError('manual choices must remain empty')) as matching:
            result = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(result['status'], 'failed')
        matching.assert_not_called()
        self.assertIn('不足',result['error'])


if __name__ == '__main__':
    unittest.main()

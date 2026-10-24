"""Retention tests move disposable JSON fixtures only; all providers stay offline."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mixstudio.service import Studio
from mixstudio.store import Store
from mixstudio.draft_history import maintain


class DraftRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trash = self.root/'recycle-fixture'; self.trash.mkdir()
        self.studio = Studio(self.root/'runtime')
        self.stub = patch('mixstudio.recycle.recycle_file', side_effect=self.recycle)
        self.mock_recycle = self.stub.start()

    def tearDown(self):
        self.studio.close(); self.stub.stop(); self.temp.cleanup()

    def recycle(self, path):
        path = Path(path)
        self.assertEqual(path.parent, self.studio.root/'draft-history')
        self.assertEqual(path.suffix, '.json')
        path.rename(self.trash/path.name)

    @staticmethod
    def draft(i):
        return {'id':f'd-{i:03}', 'created_at':i, 'title':f'文案{i}',
                'voiceover':f'可恢复的口播{i}', 'revision':1, 'asset_ids':['source']}

    def seed(self, count):
        # Existing records from an older app; avoid a callback per fixture write.
        for i in range(count):self.studio.store._put('drafts', self.draft(i)['id'], self.draft(i))

    def test_latest_fifty_kept_and_entire_old_copy_recoverable(self):
        source=self.root/'source.mp4';source.write_bytes(b'original source')
        output=self.root/'output.mp4';output.write_bytes(b'completed output')
        cache=self.studio.root/'voice-cache/shared.wav';cache.write_bytes(b'shared voice')
        self.seed(55)
        old=self.studio.store.draft('d-000')
        old.update(path=str(source),output=str(output),whole_audio={'path':str(cache)})
        self.studio.store._put('drafts',old['id'],old)
        self.studio._maintain_draft_history()
        self.assertEqual([d['id'] for d in self.studio.store.drafts()],[f'd-{i:03}' for i in range(54,4,-1)])
        self.assertEqual(len(list((self.studio.root/'draft-history').glob('*.json'))),50)
        self.assertEqual(self.mock_recycle.call_count,5)
        self.assertEqual(json.loads((self.trash/'d-000.json').read_text('utf-8')),old)
        self.assertEqual(source.read_bytes(),b'original source')
        self.assertEqual(output.read_bytes(),b'completed output')
        self.assertEqual(cache.read_bytes(),b'shared voice')

    def test_fifty_records_and_repeated_maintenance_do_not_recycle(self):
        self.seed(50);self.studio._maintain_draft_history()
        before={p.name:p.stat().st_mtime_ns for p in (self.studio.root/'draft-history').glob('*.json')}
        self.studio._maintain_draft_history()
        self.assertEqual(before,{p.name:p.stat().st_mtime_ns for p in (self.studio.root/'draft-history').glob('*.json')})
        self.mock_recycle.assert_not_called()

    def test_saving_updates_same_file_without_reordering_creation_age(self):
        self.seed(50);self.studio._maintain_draft_history()
        old=self.studio.store.draft('d-000');old.update(voiceover='编辑后的口播',revision=2)
        self.studio.store.put_draft(old)
        self.assertEqual(json.loads((self.studio.root/'draft-history/d-000.json').read_text('utf-8'))['voiceover'],'编辑后的口播')
        self.studio.store.put_draft(self.draft(50))
        self.assertIsNone(self.studio.store.draft('d-000'))
        self.assertEqual(json.loads((self.trash/'d-000.json').read_text('utf-8'))['voiceover'],'编辑后的口播')

    def test_recycle_failure_keeps_database_and_json_then_retries(self):
        self.seed(51);self.mock_recycle.side_effect=OSError('locked fixture')
        self.studio._maintain_draft_history()
        self.assertEqual(len(self.studio.store.drafts()),51)
        self.assertTrue((self.studio.root/'draft-history/d-000.json').is_file())
        self.assertIn('继续保留',self.studio.draft_history['warning'])
        self.mock_recycle.side_effect=self.recycle;self.studio._maintain_draft_history()
        self.assertEqual(len(self.studio.store.drafts()),50)
        self.assertEqual(self.studio.draft_history['warning'],'')

    def test_database_error_after_recycle_keeps_original_record(self):
        self.seed(51)
        with patch.object(self.studio.store,'remove_draft_if_current',side_effect=sqlite3.OperationalError('injected')):
            self.studio._maintain_draft_history()
        self.assertIsNotNone(self.studio.store.draft('d-000'))
        self.assertEqual(json.loads((self.trash/'d-000.json').read_text('utf-8'))['voiceover'],'可恢复的口播0')
        self.assertTrue(self.studio.draft_history['warning'])

    def test_active_job_deferred_then_finished_callback_cleans_up(self):
        self.seed(51);started=threading.Event();release=threading.Event()
        def work(event, progress):
            started.set();release.wait(3);return {'fixture':True}
        self.studio.jobs.submit('match',{'draft_id':'d-000'},work)
        self.assertTrue(started.wait(2));self.studio._maintain_draft_history()
        self.assertEqual(len(self.studio.store.drafts()),51);self.mock_recycle.assert_not_called()
        self.assertIn('任务结束',self.studio.draft_history['warning'])
        release.set()
        deadline=time.time()+3
        while self.studio.store.draft('d-000') and time.time()<deadline:time.sleep(.02)
        self.assertIsNone(self.studio.store.draft('d-000'))
        self.assertEqual(len(self.studio.store.drafts()),50)

    def test_cancelled_queued_job_triggers_cleanup(self):
        self.seed(51)
        self.studio.store.create_job('pending','match',{'draft_id':'d-000'})
        self.studio._maintain_draft_history();self.mock_recycle.assert_not_called()
        self.studio.store.update_job('pending',status='cancelled')
        self.studio.jobs.cancel('pending')
        self.assertIsNone(self.studio.store.draft('d-000'))

    def test_get_state_never_performs_cleanup_or_paid_calls(self):
        self.seed(51)
        with patch('mixstudio.draft_history.maintain',side_effect=AssertionError('GET mutation')):
            state=self.studio.state()
        self.assertEqual(len(state['drafts']),51)
        self.mock_recycle.assert_not_called()

    def test_restart_migrates_existing_database_and_preserves_records(self):
        self.seed(53);self.studio.close()
        self.studio=Studio(self.root/'runtime')
        self.assertEqual(len(self.studio.store.drafts()),50)
        self.assertEqual(self.mock_recycle.call_count,3)
        self.assertEqual(len(list((self.studio.root/'draft-history').glob('*.json'))),50)

    def test_newer_record_is_not_deleted_if_changed_during_recycle(self):
        self.seed(51)
        def changed(path):
            self.recycle(path)
            self.studio.store._put('drafts','d-000',{**self.draft(0),'voiceover':'并发修改'})
        self.mock_recycle.side_effect=changed
        self.studio._maintain_draft_history()
        self.assertEqual(self.studio.store.draft('d-000')['voiceover'],'并发修改')
        self.assertIn('最新内容继续保留',self.studio.draft_history['warning'])

    def test_path_traversal_is_preserved_not_exported_or_recycled(self):
        self.studio.store.put_draft({'id':'../outside','created_at':0,'voiceover':'保留'})
        self.assertIsNotNone(self.studio.store.draft('../outside'))
        self.assertFalse((self.studio.root/'outside.json').exists())
        self.mock_recycle.assert_not_called();self.assertTrue(self.studio.draft_history['warning'])

    def test_linked_history_directory_does_not_touch_external_files(self):
        # Deterministic link guard without requiring Windows symlink privileges.
        self.seed(51)
        original=Path.is_symlink
        def is_link(path):return path==self.studio.root/'draft-history' or original(path)
        with patch.object(Path,'is_symlink',is_link):self.studio._maintain_draft_history()
        self.assertEqual(len(self.studio.store.drafts()),51)
        self.mock_recycle.assert_not_called();self.assertTrue(self.studio.draft_history['warning'])

    def test_saved_result_survives_history_filesystem_failure(self):
        with patch('mixstudio.draft_history._directory',side_effect=PermissionError('locked fixture')):
            saved=self.studio.store.put_draft(self.draft(0))
        self.assertEqual(saved['id'],'d-000');self.assertIsNotNone(self.studio.store.draft('d-000'))
        self.assertTrue(self.studio.draft_history['warning'])


if __name__=='__main__':unittest.main()

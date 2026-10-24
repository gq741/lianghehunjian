import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from mixstudio.store import Store
from mixstudio import tasklog


class StorageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.sqlite')
        self.now = time.time()
        self.moves = []

    def file(self, name, age=40, body=b'test'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.utime(path, (self.now-age*86400, self.now-age*86400))
        return path

    def recycle(self, path):
        destination = self.root / 'mock-recycle' / str(len(self.moves))
        destination.parent.mkdir(exist_ok=True)
        Path(path).rename(destination)
        self.moves.append((Path(path), destination))

    def link_folder(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt': raise
            # A directory junction needs no developer-mode/admin symlink right.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, 'fixture junction creation failed')

    def test_inventory_counts_only_known_folders_and_default_candidates(self):
        from mixstudio.storage import inventory
        self.file('voice-cache/'+'a'*64+'.wav', body=b'voice')
        self.file('work/'+'b'*32+'/voice.wav', age=8, body=b'old')
        self.file('work/'+'c'*32+'/voice.wav', age=1, body=b'new')
        self.file('logs/tasks-2020-01-01.jsonl', body=b'logs')
        self.file('logs/private-not-a-task.txt', body=b'private')
        self.file('settings.json', body=b'settings')
        data = inventory(self.root, store=self.store, now=self.now)
        self.assertEqual([c['id'] for c in data['categories']],
                         ['voice-cache','visual-cache','semantic-cache','sdr-cache','subtitle-cache','work','logs'])
        self.assertEqual(data['total_bytes'], 5+3+3+4+7)
        self.assertEqual(data['eligible_bytes'], 3+4)
        self.assertEqual(data['cache_eligible_bytes'], 5)

    def test_confirmation_and_store_required_no_deletion(self):
        from mixstudio.storage import cleanup
        self.file('work/'+'b'*32+'/voice.wav')
        for kwargs in ({}, {'confirmed':False}, {'confirmed':1}, {'confirmed':True}):
            if kwargs.get('confirmed') is True:
                with self.assertRaises(ValueError): cleanup(self.root, **kwargs)
            else:
                with self.assertRaises(ValueError): cleanup(self.root, store=self.store, **kwargs)
        self.assertTrue((self.root/'work' / ('b'*32) / 'voice.wav').exists())

    def test_default_cleanup_only_old_temp_and_task_logs(self):
        from mixstudio.storage import cleanup
        old = self.file('work/'+'b'*32+'/voice.wav', age=8)
        recent = self.file('work/'+'c'*32+'/voice.wav', age=1)
        cache = self.file('voice-cache/'+'a'*64+'.wav')
        log = self.file('logs/tasks-2020-01-01.jsonl')
        unknown = self.file('work/user-video.mp4')
        source = self.file('source.mp4')
        draft = self.file('draft-history/old.json')
        with patch('mixstudio.storage.recycle_file', side_effect=self.recycle):
            result = cleanup(self.root, store=self.store, confirmed=True, now=self.now)
        self.assertEqual({p for p, _ in self.moves}, {old, log})
        self.assertEqual(result['recycled_files'], 2)
        self.assertFalse(result['failed'])
        for item in (recent, cache, unknown, source, draft, self.store.path):
            self.assertTrue(item.exists())

    def test_manual_caches_keep_pending_fences_and_corresponding_audio(self):
        from mixstudio.storage import cleanup
        free = self.file('voice-cache/'+'a'*64+'.wav')
        pending = self.file('voice-cache/'+'b'*64+'.pending.json')
        fenced = self.file('voice-cache/'+'b'*64+'.wav')
        other = self.file('voice-cache/notes.wav')
        with patch('mixstudio.storage.recycle_file', side_effect=self.recycle):
            result = cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        self.assertEqual([p for p, _ in self.moves], [free])
        for item in (pending, fenced, other): self.assertTrue(item.exists())
        self.assertGreaterEqual(result['inventory']['protected_files'], 2)

    def test_locked_audio_keeps_validation_sidecar(self):
        from mixstudio.storage import cleanup
        metadata = self.file('voice-cache/whole-v1-'+'a'*64+'.json', body=b'{}')
        audio = self.file('voice-cache/whole-v1-'+'a'*64+'.wav')
        with patch('mixstudio.storage.recycle_file', side_effect=OSError('occupied')) as moved:
            result = cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        moved.assert_called_once_with(audio)
        self.assertTrue(metadata.exists())
        self.assertEqual(len(result['failed']),1)

    def test_store_active_job_blocks_whole_operation(self):
        from mixstudio.storage import cleanup
        self.file('work/'+'b'*32+'/voice.wav')
        self.store.create_job('working', 'match', {})
        with patch('mixstudio.storage.recycle_file') as moved:
            with self.assertRaisesRegex(ValueError, '任务'):
                cleanup(self.root, store=self.store, confirmed=True, now=self.now)
        moved.assert_not_called()

    def test_saved_draft_references_protect_audio_sidecar_and_current_source_visual(self):
        from mixstudio.storage import cleanup
        audio = self.file('voice-cache/whole-v1-'+'a'*64+'.wav')
        sidecar = self.file('voice-cache/whole-v1-'+'a'*64+'.json', body=b'{}')
        alignment = self.file('voice-cache/alignment-'+'c'*64+'.json', body=b'{}')
        semantic = self.file('semantic-cache/semantic-'+'d'*64+'.json', body=b'{}')
        visual = self.file('visual-cache/vision-'+'e'*64+'.json',
                           body=json.dumps({'identity':{'sha256':'f'*64},'records':[]}).encode())
        unused = self.file('visual-cache/vision-'+'1'*64+'.json',
                           body=json.dumps({'identity':{'sha256':'2'*64},'records':[]}).encode())
        self.store.put_asset({'id':'source','path':str(self.root/'source.mp4'),'sha256':'f'*64})
        self.store.put_draft({'id':'draft','asset_ids':['source'],'created_at':1,
                              'whole_audio':{'path':str(audio)}})
        with patch('mixstudio.storage.recycle_file', side_effect=self.recycle):
            cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        self.assertEqual([p for p, _ in self.moves], [unused])
        for item in (audio, sidecar, alignment, semantic, visual): self.assertTrue(item.exists())

    def test_legacy_drafts_conservatively_keep_voice_cache_and_explicit_protected_paths(self):
        from mixstudio.storage import cleanup
        audio = self.file('voice-cache/'+'a'*64+'.wav')
        protected = self.file('work/'+'b'*32+'/voice.wav')
        self.store.put_draft({'id':'draft','asset_ids':[],'segments':[{'text':'hello'}],'created_at':1})
        with patch('mixstudio.storage.recycle_file', side_effect=self.recycle):
            cleanup(self.root, store=self.store, confirmed=True, include_caches=True,
                    protected_paths=[protected.parent], now=self.now)
        self.assertEqual(self.moves, [])
        self.assertTrue(audio.exists()); self.assertTrue(protected.exists())

    def test_source_inside_cache_and_sdr_references_are_preserved(self):
        from mixstudio.storage import cleanup
        source = self.file('voice-cache/'+'a'*64+'.wav')
        sdr = self.file('sdr-cache/'+'b'*64+'-sdr-v1.mp4')
        manifest = self.file('sdr-cache/'+'b'*64+'-sdr-v1.json', body=b'{}')
        self.store.put_asset({'id':'source','path':str(source),'sha256':'b'*64,
                              'prepared_media':{'path':str(sdr)}})
        self.store.put_draft({'id':'draft','asset_ids':['source'],'created_at':1})
        with patch('mixstudio.storage.recycle_file', side_effect=self.recycle):
            cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        self.assertEqual(self.moves, [])
        for item in (source,sdr,manifest): self.assertTrue(item.exists())

    def test_old_completed_outputs_under_work_and_cache_are_preserved(self):
        from mixstudio.storage import cleanup, inventory
        output_work = self.file('work/'+'a'*32+'/completed.mp4')
        output_cache = self.file('sdr-cache/'+'b'*64+'-sdr-v1.mp4')
        with self.store.connection() as db:
            for index, path in enumerate((output_work, output_cache)):
                record = {'id':f'oldrender{index}', 'kind':'render', 'status':'completed',
                          'created_at':index, 'result':{'path':str(path)}}
                db.execute('INSERT INTO jobs(id,data) VALUES(?,?)',
                           (record['id'], json.dumps(record)))
            # History outside the UI's latest 200 must remain protected too.
            for index in range(201):
                record = {'id':f'newrender{index}', 'kind':'render', 'status':'completed',
                          'created_at':index+100, 'result':{}}
                db.execute('INSERT INTO jobs(id,data) VALUES(?,?)',
                           (record['id'], json.dumps(record)))
        self.assertFalse(any(j['id'].startswith('oldrender') for j in self.store.jobs()))
        data = inventory(self.root, store=self.store, now=self.now)
        self.assertEqual(data['eligible_files'],0)
        self.assertEqual(data['cache_eligible_files'],0)
        with patch('mixstudio.storage.recycle_file') as moved:
            cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        moved.assert_not_called()
        self.assertTrue(output_work.exists()); self.assertTrue(output_cache.exists())

    def test_failed_recycle_does_not_unlink_or_hide_other_failures(self):
        from mixstudio.storage import cleanup
        first = self.file('work/'+'a'*32+'/one.wav')
        second = self.file('work/'+'b'*32+'/two.wav')
        def partial(path):
            if path == first: raise OSError('occupied')
            self.recycle(path)
        with patch('mixstudio.storage.recycle_file', side_effect=partial):
            data = cleanup(self.root, store=self.store, confirmed=True, now=self.now)
        self.assertEqual(data['recycled_files'], 1)
        self.assertEqual(len(data['failed']), 1)
        self.assertTrue(first.exists()); self.assertFalse(second.exists())

    def test_cancel_keeps_remaining_files(self):
        from mixstudio.storage import cleanup
        self.file('work/'+'a'*32+'/one.wav')
        event = threading.Event(); event.set()
        with patch('mixstudio.storage.recycle_file') as moved:
            result = cleanup(self.root, store=self.store, confirmed=True, cancel=event, now=self.now)
        moved.assert_not_called()
        self.assertTrue(result['cancelled'])

    def test_symlinked_cache_and_replaced_file_are_never_followed(self):
        from mixstudio.storage import inventory, cleanup
        outside = self.root/'outside'; outside.mkdir()
        original = outside / ('a'*64+'.wav'); original.write_bytes(b'original')
        self.link_folder(self.root/'voice-cache', outside)
        with patch('mixstudio.storage.recycle_file') as moved:
            data = inventory(self.root, store=self.store, now=self.now)
            cleanup(self.root, store=self.store, confirmed=True, include_caches=True, now=self.now)
        moved.assert_not_called()
        self.assertEqual(data['total_bytes'],0)
        self.assertEqual(original.read_bytes(), b'original')

    def test_linked_root_rejected(self):
        from mixstudio.storage import inventory
        real = self.root/'real'; real.mkdir()
        link = self.root/'linked'
        self.link_folder(link, real)
        with self.assertRaises(ValueError): inventory(link, store=self.store, now=self.now)

    def test_file_replaced_after_snapshot_is_preserved(self):
        from mixstudio import storage
        path = self.file('work/'+'b'*32+'/voice.wav')
        original_version = storage._version
        seen = 0
        def changed(file):
            nonlocal seen
            version = original_version(file)
            if file == path:
                seen += 1
                if seen == 2: return (*version[:-1], version[-1]+1)
            return version
        with patch('mixstudio.storage._version', side_effect=changed), \
             patch('mixstudio.storage.recycle_file') as moved:
            result = storage.cleanup(self.root, store=self.store, confirmed=True, now=self.now)
        moved.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual(len(result['failed']),1)

    def test_new_job_mid_cleanup_stops_remaining_files(self):
        from mixstudio.storage import cleanup
        first = self.file('work/'+'a'*32+'/one.wav')
        second = self.file('work/'+'b'*32+'/two.wav')
        def changed(path):
            self.recycle(path)
            self.store.create_job('newjob','match',{})
        with patch('mixstudio.storage.recycle_file', side_effect=changed):
            result = cleanup(self.root, store=self.store, confirmed=True, now=self.now)
        self.assertEqual(result['recycled_files'],1)
        self.assertTrue(result['warnings'])
        self.assertTrue(first.exists() or second.exists())


class TaskLogBoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def records(self):
        return [json.loads(line) for p in sorted((self.root/'logs').glob('tasks-*.jsonl'))
                for line in p.read_text('utf8').splitlines()]

    def test_progress_is_throttled_but_lifecycle_always_retained(self):
        with patch('mixstudio.tasklog.time.monotonic', return_value=100):
            for status in ['queued','running']:
                tasklog.append_event(self.root, {'id':'one','kind':'match','status':status})
            for i in range(50):
                tasklog.append_event(self.root, {'id':'one','kind':'match','status':'running','progress':i/50})
            tasklog.append_event(self.root, {'id':'one','kind':'match','status':'cancelling'})
            tasklog.append_event(self.root, {'id':'one','kind':'match','status':'cancelled'})
        self.assertEqual([r['status'] for r in self.records()], ['queued','running','cancelling','cancelled'])

    def test_key_diagnostic_change_bypasses_throttle_not_elapsed_metrics(self):
        with patch('mixstudio.tasklog.time.monotonic', return_value=100):
            for elapsed in range(20):
                tasklog.append_event(self.root, {'id':'one','kind':'match','status':'running',
                    'diagnostic':{'code':'http_503','elapsed_ms':elapsed,'vision_batch':[1,5,2]}})
            tasklog.append_event(self.root, {'id':'one','kind':'match','status':'running',
                'diagnostic':{'code':'http_503','vision_batch':[2,5,2]}})
        self.assertEqual(len(self.records()),2)

    def test_progress_after_interval_and_repeated_failure_are_kept(self):
        with patch('mixstudio.tasklog.time.monotonic', side_effect=[0,6,6,6]):
            for status in ['running','running','failed','failed']:
                tasklog.append_event(self.root, {'id':'one','kind':'match','status':status})
        self.assertEqual(len(self.records()),4)

    def test_same_day_log_rotates_before_size_limit(self):
        with patch('mixstudio.tasklog.MAX_LOG_BYTES', 256):
            for i in range(5):
                tasklog.append_event(self.root, {'id':f'job{i}','kind':'match','status':'queued'})
        paths = list((self.root/'logs').glob('tasks-*.jsonl'))
        self.assertEqual(len(paths),5)
        self.assertTrue(all(p.stat().st_size <=256 for p in paths))
        self.assertEqual(len(self.records()),5)


if __name__ == '__main__': unittest.main()

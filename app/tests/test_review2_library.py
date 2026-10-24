"""B05 isolated library identity/reconciliation tests. No cloud calls."""
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from mixstudio.service import Studio,fingerprint
from mixstudio import media

class Review2LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'library';self.folder.mkdir()
        self.studio=Studio(self.root/'runtime');self.addCleanup(self.studio.close)
        self.source=self.folder/'product.mp4'
        self.video(self.source,'red')
    def video(self,path,color):
        media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',f'color=c={color}:s=320x240:r=30:d=1','-c:v','libx264','-preset','ultrafast','-threads','1',path])
    def wait(self,job,status='completed'):
        for _ in range(500):
            current=self.studio.store.job(job['id'])
            if current['status'] in {'completed','cancelled','failed'}:
                self.assertEqual(current['status'],status,current);return current
            time.sleep(.01)
        self.fail('scan timeout')
    def scan(self):return self.wait(self.studio.scan(str(self.folder)))
    def test_replaced_and_restored_source_keeps_history_without_selectable_aliases(self):
        self.scan();old=self.studio.store.assets()[0];raw=self.source.read_bytes()
        other=self.root/'blue.mp4';self.video(other,'blue');self.source.write_bytes(other.read_bytes())
        self.scan();records=self.studio.store.assets();new=next(a for a in records if a['id']!=old['id'])
        self.assertFalse(self.studio.store.asset(old['id'])['selectable'])
        self.assertEqual(self.studio.store.asset(old['id'])['availability'],'replaced')
        self.assertTrue(new['selectable'])
        with self.assertRaises(ValueError):self.studio.asset_media_path(old['id'])
        with self.assertRaises(ValueError):self.studio.asset_media_path(old['id'],thumbnail=True)
        self.source.write_bytes(raw);self.scan()
        self.assertTrue(self.studio.store.asset(old['id'])['selectable'])
        self.assertFalse(self.studio.store.asset(new['id'])['selectable'])
        self.assertEqual(self.studio.asset_media_path(old['id']),self.source.resolve())
    def test_deleted_file_is_unselectable_but_other_directory_is_untouched(self):
        self.scan();old=self.studio.store.assets()[0]
        other_dir=self.root/'other';other_dir.mkdir();path=other_dir/'other.mp4';self.video(path,'blue')
        other=self.studio._import_one(path,threading.Event());self.studio._mark_library(other,'douyin')
        self.source.unlink();self.scan()
        self.assertFalse(self.studio.store.asset(old['id'])['selectable'])
        self.assertEqual(self.studio.store.asset(old['id'])['availability'],'missing')
        self.assertTrue(self.studio.store.asset(other['id'])['selectable'])
    def test_preview_rejects_changed_bytes_even_without_rescan(self):
        self.scan();old=self.studio.store.assets()[0]
        self.source.write_bytes(b'changed after scan')
        with self.assertRaises(ValueError):self.studio.asset_media_path(old['id'])
        self.assertFalse(self.studio.store.asset(old['id'])['selectable'])
    def test_import_failure_does_not_guess_existing_asset_is_missing(self):
        self.scan();old=self.studio.store.assets()[0]
        with patch.object(self.studio,'_import_one',side_effect=OSError('temporary read fault')):
            result=self.scan()
        self.assertEqual(len(result['result']['errors']),1)
        self.assertTrue(self.studio.store.asset(old['id'])['selectable'])
    def test_corrupt_replacement_is_invalid_and_original_bytes_restore_original_id(self):
        self.scan();old=self.studio.store.assets()[0];raw=self.source.read_bytes()
        self.source.write_bytes(b'not a video anymore');result=self.scan()
        self.assertEqual(len(result['result']['errors']),1)
        current=self.studio.store.asset(old['id'])
        self.assertFalse(current['selectable']);self.assertEqual(current['availability'],'invalid')
        self.source.write_bytes(raw);self.scan()
        self.assertTrue(self.studio.store.asset(old['id'])['selectable'])
        self.assertEqual(len(self.studio.store.assets()),1)
    def test_cancelled_scan_does_not_reconcile_missing_history(self):
        self.scan();old=self.studio.store.assets()[0];self.source.unlink()
        trigger=self.folder/'trigger.mp4';self.video(trigger,'green')
        original=self.studio._import_one
        def cancel_after_import(path,event,*args,**kwargs):
            result=original(path,event,*args,**kwargs);event.set();return result
        with patch.object(self.studio,'_import_one',side_effect=cancel_after_import):
            self.wait(self.studio.scan(str(self.folder)),status='cancelled')
        self.assertTrue(self.studio.store.asset(old['id'])['selectable'])
    def test_new_validation_protects_concurrent_download_from_stale_scan(self):
        self.scan();old=self.studio.store.assets()[0]
        revision=old['validation_revision']
        verified=self.studio._import_one(self.source,threading.Event())
        self.studio._mark_library(verified,'douyin')
        self.studio.store.reconcile_assets([dict(id=old['id'],validation_revision=revision,availability='missing')])
        current=self.studio.store.asset(old['id'])
        self.assertTrue(current['selectable']);self.assertIn('douyin',current['libraries'])
        self.assertGreater(current['validation_revision'],revision)
    def test_validation_preserves_transcript_and_labels_added_after_import_snapshot(self):
        self.scan();old=self.studio.store.assets()[0]
        clips=[dict(start=0,end=1,text='人工转写',clip_id='clip')]
        self.studio.store.set_asset_transcript(old['id'],clips)
        self.studio._mark_library(old,'douyin')
        activated=self.studio.store.activate_asset(old)
        self.assertEqual(activated['clips'],clips)
        self.assertEqual(activated['transcript_status'],'ready')
        self.assertIn('douyin',activated['libraries'])
    def test_stale_library_snapshot_cannot_restore_invalid_asset_or_old_revision(self):
        self.scan();old=self.studio.store.assets()[0]
        fresh=self.studio._import_one(self.source,threading.Event())
        self.studio.store.reconcile_assets([dict(id=old['id'],validation_revision=fresh['validation_revision'],availability='missing')])
        with patch.object(self.studio.store,'put_asset',side_effect=AssertionError('no stale whole-row write')):
            marked=self.studio._mark_library(old,'douyin')
        self.assertFalse(marked['selectable']);self.assertEqual(marked['validation_revision'],fresh['validation_revision'])
        self.assertIn('local',marked['libraries']);self.assertIn('douyin',marked['libraries'])
    def test_concurrent_restore_and_download_validation_wins_over_scan_missing_snapshot(self):
        self.scan();old=self.studio.store.assets()[0];raw=self.source.read_bytes();self.source.unlink()
        entered=threading.Event();release=threading.Event();original=self.studio.store.reconcile_assets
        def paused(changes):
            entered.set()
            if not release.wait(5):raise AssertionError('reconcile release timeout')
            return original(changes)
        with patch.object(self.studio.store,'reconcile_assets',side_effect=paused):
            job=self.studio.scan(str(self.folder))
            try:
                self.assertTrue(entered.wait(3));self.source.write_bytes(raw)
                imported=self.studio._import_one(self.source,threading.Event())
                self.studio._mark_library(imported,'douyin')
            finally:release.set()
            self.wait(job)
        current=self.studio.store.asset(old['id'])
        self.assertTrue(current['selectable']);self.assertIn('douyin',current['libraries'])
    def test_missing_identity_never_bypasses_preview_validation(self):
        self.studio.store.put_asset(dict(id='legacy',path=str(self.source)))
        with self.assertRaises(ValueError):self.studio.asset_media_path('legacy')
    def test_state_does_not_rehash_source_files(self):
        self.scan()
        with patch('mixstudio.service.fingerprint',side_effect=AssertionError('state should not hash')):
            self.studio.state()

if __name__=='__main__':unittest.main()

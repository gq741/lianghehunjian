"""Owned disposable music-library fixtures; no paid services or user files."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import subprocess
import time
import unittest
import wave
from unittest.mock import patch

try:
    from mixstudio.music_library import MusicLibrary
except ImportError:
    MusicLibrary=None
from mixstudio import media
from mixstudio.jobs import Cancelled


@unittest.skipUnless(shutil.which('ffprobe'),'FFprobe required')
class MusicLibraryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(MusicLibrary,'Persistent music library has not been implemented')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.runtime=self.root/'runtime';self.sources=self.root/'sources';self.sources.mkdir()
        self.library=MusicLibrary(self.runtime)
        self.moves=[]

    def wav(self,name='歌曲.wav',tone=1,frames=4800):
        path=self.sources/name
        with wave.open(str(path),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(48000)
            output.writeframes(int(tone).to_bytes(2,'little',signed=True)*frames)
        return path

    def scan(self):return self.library.scan_directory(str(self.sources))

    def imported(self,path=None):
        path=path or self.wav();scan=self.scan()
        chosen=next(c for c in scan['candidates'] if c['name']==path.name)
        result=self.library.import_candidates(scan['scan_id'],[chosen['id']])
        self.assertFalse(result['failed'],result)
        return result['imported'][0]

    def recycle(self,path):
        target=self.root/'recycled';target.mkdir(exist_ok=True)
        dest=target/(str(len(self.moves))+Path(path).suffix)
        Path(path).rename(dest);self.moves.append((Path(path),dest))

    def test_scan_is_direct_audio_only_with_opaque_identifiers(self):
        self.wav();(self.sources/'notes.txt').write_text('ignore');(self.sources/'nested').mkdir()
        self.wav('upper.WAV');shutil.copyfile(self.sources/'upper.WAV',self.sources/'nested'/'nested.wav')
        scan=self.scan()
        self.assertEqual({c['name'] for c in scan['candidates']},{'歌曲.wav','upper.WAV'})
        self.assertTrue(all(set(c)=={'id','name','size'} for c in scan['candidates']))
        self.assertNotIn(str(self.sources),json.dumps(scan))

    def test_only_selected_audio_is_copied_and_original_can_move_after_import(self):
        source=self.wav();other=self.wav('保留.wav',2);original=source.read_bytes()
        track=self.imported(source);copied=self.library.resolve(track['id'])
        self.assertNotEqual(copied,source);self.assertEqual(copied.read_bytes(),original)
        source.rename(self.root/'moved-original.wav')
        self.assertEqual(copied.read_bytes(),original);self.assertTrue(other.exists())
        self.assertTrue(self.library.list_tracks()[0]['available'])

    def test_restart_retains_name_id_and_owned_copy(self):
        track=self.imported();reopened=MusicLibrary(self.runtime)
        saved=reopened.list_tracks()[0]
        self.assertEqual(saved['id'],track['id']);self.assertEqual(saved['name'],'歌曲.wav')
        self.assertEqual(reopened.resolve(track['id']),self.library.resolve(track['id']))

    def test_identical_content_deduplicates_even_with_another_name(self):
        source=self.wav();first=self.imported(source)
        copied=self.sources/'重命名.wav';shutil.copyfile(source,copied)
        scan=self.scan();ids=[c['id'] for c in scan['candidates']]
        result=self.library.import_candidates(scan['scan_id'],ids)
        self.assertEqual(result['imported'],[]);self.assertEqual(len(result['duplicates']),2)
        self.assertEqual(len(self.library.list_tracks()),1)
        self.assertEqual(self.library.list_tracks()[0]['id'],first['id'])

    def test_same_name_different_music_is_kept_separately(self):
        source=self.wav();first=self.imported(source);source.unlink();self.wav(tone=2)
        second=self.imported(source)
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(len(self.library.list_tracks()),2)

    def test_probe_rejects_fake_audio_without_success_or_copy(self):
        source=self.sources/'fake.mp3';source.write_bytes(b'not audio')
        scan=self.scan();result=self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']])
        self.assertEqual(result['imported'],[]);self.assertEqual(len(result['failed']),1)
        self.assertEqual(self.library.list_tracks(),[]);self.assertEqual(source.read_bytes(),b'not audio')

    def test_unknown_candidate_and_traversal_ids_rejected_before_copy(self):
        self.wav();scan=self.scan()
        for candidate in ['../outside.wav','unknown',str(self.sources/'歌曲.wav')]:
            with self.assertRaises(ValueError):self.library.import_candidates(scan['scan_id'],[candidate])
        with self.assertRaises(ValueError):self.library.resolve('../outside.wav')

    def test_expired_scan_requires_rescan(self):
        self.wav()
        with patch('time.time',return_value=1000):scan=self.scan()
        with patch('time.time',return_value=1000+86400):
            with self.assertRaisesRegex(ValueError,'过期|重新扫描'):
                self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']])

    def test_changed_since_scan_is_not_imported(self):
        source=self.wav();scan=self.scan();self.wav(tone=2)
        result=self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']])
        self.assertEqual(result['imported'],[]);self.assertEqual(len(result['failed']),1)
        self.assertEqual(self.library.list_tracks(),[])

    def test_change_during_copy_leaves_no_success_and_can_rescan(self):
        source=self.wav();scan=self.scan();copy=self.library._copy_file
        def changing(original,stage,**kwargs):
            result=copy(original,stage,**kwargs);self.wav(tone=3);return result
        with patch.object(self.library,'_copy_file',side_effect=changing):
            result=self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']])
        self.assertEqual(result['imported'],[]);self.assertEqual(len(result['failed']),1)
        self.assertEqual(self.library.list_tracks(),[])
        self.assertFalse(list((self.runtime/'music-library').glob('.music-import-*.tmp')))
        self.imported(source)

    def test_manifest_write_failure_is_not_reported_as_imported_and_retry_recovers_copy(self):
        self.wav();scan=self.scan();chosen=[scan['candidates'][0]['id']]
        with patch.object(self.library,'_save',side_effect=OSError('disk fixture')):
            result=self.library.import_candidates(scan['scan_id'],chosen)
        self.assertFalse(result['imported']);self.assertEqual(len(result['failed']),1)
        self.assertEqual(self.library.list_tracks(),[])
        result=self.library.import_candidates(scan['scan_id'],chosen)
        self.assertEqual(len(result['imported']),1);self.assertFalse(result['failed'])
        self.assertEqual(len(list((self.runtime/'music-library').glob('*.wav'))),1)

    def test_corrupt_manifest_is_preserved_and_not_overwritten(self):
        track=self.imported();manifest=self.runtime/'music-library'/'manifest.json'
        broken=b'{broken';manifest.write_bytes(broken)
        reopened=MusicLibrary(self.runtime)
        with self.assertRaises(ValueError):reopened.list_tracks()
        with self.assertRaises(ValueError):reopened.delete([track['id']])
        self.assertEqual(manifest.read_bytes(),broken)

    def test_manifest_cannot_point_outside_owned_directory(self):
        self.imported();manifest=self.runtime/'music-library'/'manifest.json';saved=json.loads(manifest.read_text('utf8'))
        saved['tracks'][0]['file']='../outside.wav';manifest.write_text(json.dumps(saved),'utf8')
        with self.assertRaises(ValueError):self.library.list_tracks()

    def test_delete_recycles_copy_not_original_and_persists(self):
        source=self.wav();original=source.read_bytes();track=self.imported(source);owned=self.library.resolve(track['id'])
        with patch('mixstudio.music_library.recycle_file',side_effect=self.recycle):result=self.library.delete([track['id']])
        self.assertEqual(result['removed_ids'],[track['id']]);self.assertFalse(owned.exists())
        self.assertEqual(source.read_bytes(),original);self.assertEqual(MusicLibrary(self.runtime).list_tracks(),[])

    def test_failed_recycle_keeps_manifest_and_song(self):
        track=self.imported();manifest=self.runtime/'music-library'/'manifest.json';original=manifest.read_bytes()
        with patch('mixstudio.music_library.recycle_file',side_effect=OSError('locked')):result=self.library.delete([track['id']])
        self.assertEqual(result['removed_ids'],[]);self.assertEqual(len(result['failed']),1)
        self.assertEqual(manifest.read_bytes(),original);self.assertTrue(self.library.resolve(track['id']).exists())

    def test_active_path_is_protected(self):
        track=self.imported();owned=self.library.resolve(track['id'])
        with patch('mixstudio.music_library.recycle_file') as move:
            result=self.library.delete([track['id']],protected_paths=[owned])
        self.assertEqual(result['removed_ids'],[]);self.assertTrue(result['failed']);move.assert_not_called()

    def test_missing_owned_copy_is_listed_unavailable_and_can_remove_record(self):
        track=self.imported();owned=self.library.resolve(track['id']);owned.unlink()
        self.assertEqual(self.library.resolve(track['id']),owned)
        self.assertFalse(self.library.list_tracks()[0]['available'])
        with patch('mixstudio.music_library.recycle_file') as move:result=self.library.delete([track['id']])
        self.assertEqual(result['removed_ids'],[track['id']]);move.assert_not_called()

    def test_cancelled_copy_removes_only_staging_and_preserves_committed_tracks(self):
        first=self.imported();second=self.wav('第二首.wav',tone=2,frames=1000000)
        scan=self.scan();candidate=next(c for c in scan['candidates'] if c['name']==second.name)
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):self.library.import_candidates(scan['scan_id'],[candidate['id']],cancel=event)
        self.assertEqual([t['id'] for t in self.library.list_tracks()],[first['id']]);self.assertTrue(second.exists())

    def test_cancel_is_observed_between_copy_chunks(self):
        source=self.wav(frames=2000000);scan=self.scan()
        class Event:
            calls=0
            def is_set(self):self.calls+=1;return self.calls>=4
        with self.assertRaises(Cancelled):
            self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']],cancel=Event())
        self.assertEqual(self.library.list_tracks(),[])
        self.assertFalse(list((self.runtime/'music-library').glob('.music-import-*.tmp')))
        self.assertTrue(source.exists())

    def test_cancel_after_one_commit_keeps_that_track(self):
        self.wav('a.wav');self.wav('b.wav',2);scan=self.scan();event=threading.Event()
        def progress(value,message):event.set()
        with self.assertRaises(Cancelled):
            self.library.import_candidates(scan['scan_id'],[c['id'] for c in scan['candidates']],cancel=event,progress=progress)
        self.assertEqual([t['name'] for t in self.library.list_tracks()],['a.wav'])
        self.assertEqual(len(MusicLibrary(self.runtime).list_tracks()),1)

    def test_listing_does_not_wait_for_copy(self):
        self.wav();scan=self.scan();entered=threading.Event();release=threading.Event();copy=self.library._copy_file
        def blocking(*args,**kwargs):entered.set();release.wait(5);return copy(*args,**kwargs)
        results=[]
        with patch.object(self.library,'_copy_file',side_effect=blocking):
            thread=threading.Thread(target=lambda:results.append(self.library.import_candidates(scan['scan_id'],[scan['candidates'][0]['id']])))
            thread.start()
            try:
                self.assertTrue(entered.wait(3));started=time.monotonic()
                self.assertEqual(self.library.list_tracks(),[])
                self.assertLess(time.monotonic()-started,1.)
            finally:release.set();thread.join(5)
        self.assertEqual(len(results[0]['imported']),1)

    def test_recycle_then_manifest_failure_is_recoverable_without_second_recycle(self):
        track=self.imported()
        with patch('mixstudio.music_library.recycle_file',side_effect=self.recycle), \
             patch.object(self.library,'_save',side_effect=OSError('disk')):
            result=self.library.delete([track['id']])
        self.assertFalse(result['removed_ids']);self.assertTrue(result['failed'][0]['moved_to_recycle_bin'])
        self.assertFalse(self.library.list_tracks()[0]['available'])
        with patch('mixstudio.music_library.recycle_file') as move:result=self.library.delete([track['id']])
        move.assert_not_called();self.assertEqual(result['removed_ids'],[track['id']])

    def test_junction_root_and_source_directory_are_rejected(self):
        if os.name!='nt':self.skipTest('Windows directory junction regression')
        actual=self.root/'outside';actual.mkdir();runtime=self.root/'junction-runtime';runtime.mkdir()
        link=runtime/'music-library'
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(actual)],capture_output=True)
        self.assertEqual(result.returncode,0)
        with self.assertRaises(ValueError):MusicLibrary(runtime)
        with self.assertRaises(ValueError):self.library.scan_directory(link)
        self.assertEqual(list(actual.iterdir()),[])

    def test_owned_file_replaced_by_directory_junction_is_unavailable_not_recycled(self):
        if os.name!='nt':self.skipTest('Windows directory junction regression')
        track=self.imported();owned=self.library.resolve(track['id']);owned.unlink()
        outside=self.root/'outside';outside.mkdir();original=outside/'private.txt';original.write_text('preserve','utf8')
        result=subprocess.run(['cmd','/c','mklink','/J',str(owned),str(outside)],capture_output=True)
        self.assertEqual(result.returncode,0)
        self.assertFalse(self.library.list_tracks()[0]['available'])
        with patch('mixstudio.music_library.recycle_file') as move:result=self.library.delete([track['id']])
        move.assert_not_called();self.assertFalse(result['removed_ids']);self.assertEqual(original.read_text('utf8'),'preserve')

    def test_library_directory_is_not_treated_as_an_original_source_directory(self):
        self.imported()
        with self.assertRaisesRegex(ValueError,'原歌曲目录'):
            self.library.scan_directory(self.runtime/'music-library')
        intermediary=self.runtime/'empty';intermediary.mkdir()
        with self.assertRaisesRegex(ValueError,'原歌曲目录'):
            self.library.scan_directory(intermediary/'..'/'music-library')

    def test_manifest_size_and_track_limits_preserve_readable_old_manifest(self):
        self.imported();manifest=self.runtime/'music-library'/'manifest.json';before=manifest.read_bytes()
        template=json.loads(before)['tracks'][0]
        many=[{**template,'id':f'{i:064x}','sha256':f'{i:064x}','file':f'{i:064x}.wav','name':'歌'*255} for i in range(4500)]
        self.assertGreater(len(json.dumps({'version':1,'tracks':many},ensure_ascii=False).encode('utf8')),4*1024*1024)
        with self.assertRaises(ValueError):self.library._save(many)
        self.assertEqual(manifest.read_bytes(),before)
        with self.assertRaises(ValueError):self.library._save([template]*10001)
        self.assertEqual(manifest.read_bytes(),before);self.assertEqual(len(self.library.list_tracks()),1)

    def test_capacity_failure_does_not_report_import_success_and_copy_is_recoverable(self):
        self.wav();scan=self.scan();ids=[scan['candidates'][0]['id']]
        with patch('mixstudio.music_library.MAX_MANIFEST_BYTES',128):
            result=self.library.import_candidates(scan['scan_id'],ids)
        self.assertFalse(result['imported']);self.assertTrue(result['failed'])
        self.assertFalse((self.runtime/'music-library'/'manifest.json').exists())
        result=self.library.import_candidates(scan['scan_id'],ids)
        self.assertEqual(len(result['imported']),1);self.assertEqual(len(self.library.list_tracks()),1)

    def test_symlinked_source_and_owned_file_are_never_followed(self):
        source=self.wav();link=self.sources/'link.wav'
        try:link.symlink_to(source)
        except OSError:self.skipTest('file symlinks require developer mode')
        scan=self.scan();self.assertNotIn('link.wav',[c['name'] for c in scan['candidates']])
        track=self.imported(source);owned=self.library.resolve(track['id']);owned.unlink();owned.symlink_to(source)
        self.assertFalse(self.library.list_tracks()[0]['available'])
        with patch('mixstudio.music_library.recycle_file') as move:result=self.library.delete([track['id']])
        self.assertEqual(result['removed_ids'],[]);move.assert_not_called();self.assertTrue(source.exists())


if __name__=='__main__':unittest.main()

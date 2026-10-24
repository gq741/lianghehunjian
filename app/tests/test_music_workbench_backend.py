"""Music workbench integration: owned copies, no provider calls."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from mixstudio.service import Studio
from mixstudio.http_app import create_app
from mixstudio import providers


def song(path, value=0):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
        out.writeframes(int(value).to_bytes(2,'little',signed=True)*2000)


class MusicWorkbenchBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.folder=self.root/'songs';self.folder.mkdir();song(self.folder/'甲.wav')
        self.studio=Studio(self.root/'runtime')
        self.network=patch.object(providers,'_post',side_effect=AssertionError('paid call forbidden'));self.network.start()
        self.app=create_app(self.studio);self.client=self.app.test_client();self.client.get('/')
        self.headers={'X-Local-Token':self.app.config['LOCAL_TOKEN']}

    def tearDown(self):
        self.studio.close();self.network.stop();self.tmp.cleanup()

    def request(self,path,data=None):
        response=self.client.get(path,headers=self.headers) if data is None else self.client.post(path,json=data,headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        return response.json['data']

    def wait(self,job):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            current=self.studio.store.job(job['id'])
            if current['status'] not in {'queued','running','cancelling'}:return current
            time.sleep(.01)
        self.fail('local import timeout')

    def import_songs(self):
        scan=self.request('/api/music/scan',{'folder':str(self.folder)})
        job=self.request('/api/music/import',{'scan_id':scan['scan_id'],'candidate_ids':[c['id'] for c in scan['candidates']]})
        completed=self.wait(job);self.assertEqual(completed['status'],'completed',completed)
        return self.request('/api/music')

    def test_import_selected_song_survives_original_removal_and_restart(self):
        state=self.import_songs();self.assertEqual(len(state['tracks']),1)
        current=state['current'];self.assertTrue(current['managed']);self.assertTrue(current['available'])
        self.assertNotEqual(Path(current['path']),self.folder/'甲.wav')
        (self.folder/'甲.wav').unlink()
        self.studio.close();self.studio=Studio(self.root/'runtime')
        value=self.studio.music_state()
        self.assertEqual(value['current']['id'],current['id']);self.assertTrue(value['current']['available'])
        self.assertEqual(value['error'],'')

    def test_endpoints_require_session_and_delete_confirmation(self):
        for path,data in [('/api/music/scan',{'folder':str(self.folder)}),('/api/music/import',{}),
                ('/api/music/select',{'track_id':None}),('/api/music/delete',{'track_ids':[]}),('/api/music/volume',{'gain':.1})]:
            with self.subTest(path=path):self.assertEqual(self.client.post(path,json=data).status_code,403)
        state=self.import_songs();identifier=state['current']['id'];file=Path(state['current']['path'])
        response=self.client.post('/api/music/delete',json={'track_ids':[identifier],'confirm':False},headers=self.headers)
        self.assertEqual(response.status_code,400);self.assertTrue(file.is_file())

    def test_delete_selected_owned_copy_closes_music_and_keeps_original(self):
        state=self.import_songs();identifier=state['current']['id'];file=Path(state['current']['path'])
        def recycle(path):
            Path(path).unlink();return True
        with patch('mixstudio.music_library.recycle_file',side_effect=recycle):
            result=self.request('/api/music/delete',{'track_ids':[identifier],'confirm':True})
        self.assertEqual(result['removed_ids'],[identifier]);self.assertIsNone(result['current'])
        self.assertEqual(self.studio.settings.public()['bgm_path'],'')
        self.assertFalse(file.exists());self.assertTrue((self.folder/'甲.wav').exists())

    def test_selected_copy_missing_is_reported_without_switching(self):
        state=self.import_songs();path=Path(state['current']['path']);path.unlink()
        current=self.request('/api/music')['current']
        self.assertEqual(current['id'],state['current']['id']);self.assertFalse(current['available'])
        self.assertTrue(self.request('/api/music')['error'])
        summary=self.request('/api/state')['music'];self.assertTrue(summary['error']);self.assertNotIn('tracks',summary)
        self.assertEqual(self.studio.settings.public()['bgm_path'],str(path))

    def test_volume_updates_only_music_preference(self):
        before=self.studio.settings.public();self.request('/api/music/volume',{'gain':.3})
        after=self.studio.settings.public();self.assertEqual(after['bgm_gain'],.3)
        for name in ('target_seconds','output_count','voice_id','bgm_path'):self.assertEqual(after[name],before[name])
        for value in (True,-1,1.1,'invalid'):
            response=self.client.post('/api/music/volume',json={'gain':value},headers=self.headers)
            self.assertEqual(response.status_code,400)

    def test_import_does_not_change_an_existing_default(self):
        state=self.import_songs();identifier=state['current']['id']
        song(self.folder/'乙.wav',3);new=self.import_songs()
        self.assertEqual(len(new['tracks']),2);self.assertEqual(new['current']['id'],identifier)

    def test_import_does_not_hold_global_action_lock(self):
        scan=self.request('/api/music/scan',{'folder':str(self.folder)})
        entered=threading.Event();release=threading.Event()
        original=self.studio.music_library.import_candidates
        def slow(*args,**kwargs):
            entered.set();release.wait(3);return original(*args,**kwargs)
        try:
            with patch.object(self.studio.music_library,'import_candidates',side_effect=slow):
                job=self.request('/api/music/import',{'scan_id':scan['scan_id'],'candidate_ids':[c['id'] for c in scan['candidates']]})
                self.assertTrue(entered.wait(2))
                acquired=self.studio.action_lock.acquire(timeout=.2)
                if acquired:self.studio.action_lock.release()
                self.assertTrue(acquired)
                release.set();self.assertEqual(self.wait(job)['status'],'completed')
        finally:release.set()

    def test_active_production_blocks_music_deletion(self):
        state=self.import_songs();identifier=state['current']['id'];path=Path(state['current']['path'])
        self.studio.store.create_job('active','render',{'draft_id':'owned-fixture'})
        response=self.client.post('/api/music/delete',json={'track_ids':[identifier],'confirm':True},headers=self.headers)
        self.assertEqual(response.status_code,400);self.assertTrue(path.exists())
        self.studio.store.update_job('active',status='cancelled')

    def test_bad_manifest_does_not_break_main_state(self):
        self.import_songs()
        (self.studio.root/'music-library'/'manifest.json').write_text('{broken',encoding='utf-8')
        state=self.request('/api/state')
        self.assertIn('assets',state);self.assertTrue(state['music']['error'])
        self.assertEqual((self.studio.root/'music-library'/'manifest.json').read_text(),'{broken')

    def test_explicit_no_music_is_not_overridden_by_import(self):
        self.request('/api/music/select',{'track_id':None})
        state=self.import_songs();self.assertIsNone(state['current'])
        self.assertEqual(len(state['tracks']),1)

    def test_invalid_audio_import_is_failed_not_a_fake_success(self):
        (self.folder/'甲.wav').write_bytes(b'not audio')
        scan=self.request('/api/music/scan',{'folder':str(self.folder)})
        completed=self.wait(self.request('/api/music/import',{'scan_id':scan['scan_id'],
            'candidate_ids':[c['id'] for c in scan['candidates']]}))
        self.assertEqual(completed['status'],'failed',completed)
        self.assertIn('未导入',completed['error'])
        self.assertEqual(self.request('/api/music')['tracks'],[])

    def test_same_size_music_replacement_is_rejected_before_selection_or_production(self):
        state=self.import_songs();current=state['current'];path=Path(current['path'])
        before=path.stat().st_size;song(path,5);self.assertEqual(path.stat().st_size,before)
        response=self.client.post('/api/music/select',json={'track_id':current['id']},headers=self.headers)
        self.assertEqual(response.status_code,400)
        status=self.request('/api/music');self.assertFalse(status['current']['available']);self.assertIn('修改',status['error'])
        with self.assertRaisesRegex(ValueError,'修改'):
            self.studio.require_music(self.studio.settings.public())


if __name__=='__main__':unittest.main()

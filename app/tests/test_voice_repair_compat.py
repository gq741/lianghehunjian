"""Disposable runtime tests for voice-first state and compatibility routes."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio.service import Studio
from mixstudio.http_app import create_app


class VoiceRepairCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = Studio(Path(self.temp.name)/'runtime')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.studio.close)
        self.draft = {'id':'d','workflow':'script_first_v2','production_mode':'whole_voice_v1',
            'voiceover':'看看商品。','segments':[{'segment_id':'s1','text':'看看商品。'}],
            'target_seconds':25,'asset_ids':[],'revision':1,'created_at':1,
            'match_status':'pending','status':'draft','clips':[],'matches':[]}
        self.studio.store.put_draft(self.draft)
        self.app = create_app(self.studio)
        self.client = self.app.test_client()
        self.client.get('/')
        self.headers = {'X-Local-Token':self.app.config['LOCAL_TOKEN']}

    def test_compact_state_includes_preflight_scope_and_time(self):
        result={'status':'ready','asset_ids':[],'source_hashes':[],'checked_at':12345,
                'message':'本地检查通过，不保证远端账户余额'}
        self.studio.store.meta('free_preflight',result)
        self.studio.store.create_job('pre','preflight',{'asset_ids':[]})
        self.studio.store.update_job('pre',status='completed',result=result,message='处理完成')
        data=self.studio.state(compact=True)
        self.assertEqual(data['preflight'],result)
        self.assertEqual(data['jobs'][0]['result']['checked_at'],12345)
        self.assertEqual(data['jobs'][0]['result']['message'],result['message'])

    def test_legacy_render_endpoint_redirects_whole_voice_with_original_consent(self):
        with patch.object(self.studio,'produce_whole',return_value={'id':'whole-job'}) as produce:
            response=self.client.post('/api/drafts/d/render',headers=self.headers,json={'allow_paid':True})
        self.assertEqual(response.status_code,200)
        produce.assert_called_once_with('d',True,retry_unknown=False)
        with patch.object(self.studio,'produce_whole') as produce:
            response=self.client.post('/api/drafts/d/render',headers=self.headers,json={'allow_paid':False})
        self.assertEqual(response.status_code,400)
        produce.assert_not_called()

    def test_genuine_legacy_render_does_not_redirect(self):
        self.studio.store.put_draft({**self.draft,'production_mode':'legacy_v2'})
        with patch.object(self.studio,'produce_whole') as produce,patch.object(self.studio,'bound_v2',side_effect=ValueError('legacy-path')) as bound:
            with self.assertRaisesRegex(ValueError,'legacy-path'):
                self.studio.render('d',True)
        bound.assert_called_once();produce.assert_not_called()

    def test_realign_route_is_authenticated_and_has_no_paid_consent(self):
        self.assertNotEqual(self.client.post('/api/v2/drafts/d/realign',json={'refresh':True}).status_code,200)
        with patch.object(self.studio,'realign_whole',create=True,return_value={'id':'align'}) as align:
            response=self.client.post('/api/v2/drafts/d/realign',headers=self.headers,json={'refresh':True})
        self.assertEqual(response.status_code,200)
        align.assert_called_once_with('d',refresh=True)

    def audio(self):
        path=self.studio.root/'voice-cache'/'fixture.wav'
        path.write_bytes(b'owned disposable wav fixture'*20)
        signature=hashlib.sha256(path.read_bytes()).hexdigest()
        self.studio.store.put_draft({**self.draft,'whole_audio':{'path':str(path),'key':'fixture-key','sha256':signature,'duration':1.}})
        return path

    def test_range_audio_uses_one_hash_per_file_version_and_rejects_changed_bytes(self):
        from mixstudio.whole_voice import _hash
        path=self.audio()
        with patch('mixstudio.whole_voice._hash',wraps=_hash) as compute:
            for start in [0,10,30]:
                response=self.client.get('/media/voices/d?key=fixture-key',headers={'Range':f'bytes={start}-{start+9}'})
                self.assertEqual(response.status_code,206);response.close()
            self.assertEqual(compute.call_count,1)
            path.write_bytes(b'changed untrusted audio'*10)
            response=self.client.get('/media/voices/d?key=fixture-key',headers={'Range':'bytes=0-9'})
            self.assertEqual(response.status_code,400)
            self.assertEqual(compute.call_count,2)

    def test_old_audio_key_is_not_served_as_new_audio(self):
        self.audio()
        response=self.client.get('/media/voices/d?key=previous-voice')
        self.assertEqual(response.status_code,400)

    def test_active_alignment_is_exposed_as_draft_job_in_compact_state(self):
        self.studio.store.create_job('alignment','alignment',{'draft_id':'d','revision':1})
        data=self.studio.state(compact=True)
        self.assertEqual(data['jobs'][0]['draft_id'],'d')

    def original_asset(self):
        from mixstudio import preprocess
        from mixstudio.media_identity import file_version
        path=Path(self.temp.name)/'owned-source.mp4'
        path.write_bytes(b'disposable descriptor-only source')
        signature=hashlib.sha256(path.read_bytes()).hexdigest()
        record={'version':preprocess.VERSION,'mode':'original','source_sha256':signature,
                'time_offset':0.,'probe_version':preprocess.PROBE_VERSION,
                'source_version':list(file_version(path))}
        asset={'id':'source','path':str(path),'sha256':signature,'prepared_media':record,'selectable':True}
        self.studio.store.put_asset(asset)
        return asset,record

    def test_current_version_bound_original_descriptor_skips_reprobe(self):
        asset,record=self.original_asset()
        with patch('mixstudio.preprocess.prepare') as prepare:
            self.assertEqual(self.studio._prepare_asset(asset),asset)
        prepare.assert_not_called()

    def test_legacy_original_or_changed_version_requires_fresh_probe(self):
        asset,record=self.original_asset()
        for replacement in ({k:v for k,v in record.items() if k!='probe_version'},
                            {**record,'source_version':[0,0,0,0,0]}):
            with self.subTest(record=replacement),patch('mixstudio.preprocess.prepare',return_value=record) as prepare:
                self.studio._prepare_asset({**asset,'prepared_media':replacement})
            prepare.assert_called_once()


if __name__=='__main__':unittest.main()

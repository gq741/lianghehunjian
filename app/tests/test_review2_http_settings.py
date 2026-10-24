"""Second audit regressions: local HTTP bounds and explicit credential recovery."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
if os.environ.get('JDMIX_REVIEW2_SOURCE'):
    sys.path.insert(0, os.environ['JDMIX_REVIEW2_SOURCE'])
from mixstudio.http_app import create_app
from mixstudio.settings import Settings, _dpapi
from mixstudio.v2 import V2Actions

class FakeStudio:
    def __init__(self):
        self.calls=[]
        self.settings=SimpleNamespace(update=lambda data: data)
    def replace_matches_v2(self, identifier, matches):
        self.calls.append(matches)
        return {'id':identifier,'segments':len(matches),'first_ids':matches[0]['clip_ids'][:2]}

class CandidateTransportTests(unittest.TestCase):
    def setUp(self):
        self.studio=FakeStudio();self.app=create_app(self.studio,token='review-only-token');self.client=self.app.test_client()
        self.headers={'X-Local-Token':'review-only-token','Content-Type':'application/json'}
    def put(self,data):
        return self.client.put('/api/v2/drafts/test/matches',data=json.dumps(data,ensure_ascii=False,separators=(',',':')),headers=self.headers)
    def test_original_558kb_legacy_pool_remains_supported(self):
        ids=[f'{i:024x}' for i in range(860)]
        matches=[{'segment_id':f's{i}','clip_ids':ids} for i in range(24)]
        matches[0]['clip_ids']=ids[1:]
        self.assertGreater(len(json.dumps({'matches':matches}).encode()),524288)
        response=self.put({'matches':matches})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.studio.calls,[matches])
    def test_compact_full_64_by_4500_pool_preserves_order(self):
        ids=[f'{i:024x}' for i in range(4500)]
        indices=list(range(4499,-1,-1))
        response=self.put({'clip_table':ids,'matches':[{'segment_id':f's{i}','clip_indices':indices} for i in range(64)]})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(len(self.studio.calls[0]),64)
        self.assertEqual(self.studio.calls[0][0]['clip_ids'],ids[::-1])
    def test_compact_worst_supported_escaped_id_size_fits_bound(self):
        ids=['画'*194+f'{i:06d}' for i in range(4500)]
        payload={'clip_table':ids,'matches':[{'segment_id':f's{i}','clip_indices':list(range(4500))} for i in range(64)]}
        raw=json.dumps(payload,ensure_ascii=True,separators=(',',':'))
        self.assertLess(len(raw),8*1024*1024)
        response=self.client.put('/api/v2/drafts/test/matches',data=raw,headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.studio.calls[0][0]['clip_ids'],ids)
    def test_empty_pool_round_trip(self):
        response=self.put({'clip_table':[],'matches':[{'segment_id':'s1','clip_indices':[]}]})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.studio.calls[0],[{'segment_id':'s1','clip_ids':[]}])
    def test_invalid_compact_indices_or_shape_never_reaches_handler(self):
        cases=[[-1],[1],[True],[.0],['0'],[0,0]]
        for indices in cases:
            with self.subTest(indices=indices):
                self.assertEqual(self.put({'clip_table':['c1'],'matches':[{'segment_id':'s1','clip_indices':indices}]}).status_code,400)
        for value in [{'clip_table':['c1','c1'],'matches':[{'segment_id':'s1','clip_indices':[]}]},
                      {'clip_table':['c1'],'matches':[{'segment_id':'s1','clip_ids':['c1']}]},
                      {'clip_table':['c1'],'matches':[{'segment_id':'s1','clip_indices':[0]}]*65}]:
            self.assertEqual(self.put(value).status_code,400)
        self.assertEqual(self.studio.calls,[])
    def test_over_bound_is_413_and_not_dispatched(self):
        raw=' '*(8*1024*1024+1)
        response=self.client.put('/api/v2/drafts/test/matches',data=raw,headers=self.headers)
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.studio.calls,[])
    def test_default_limit_and_authentication_remain(self):
        large=' '*600000
        response=self.client.put('/api/settings',data=large,headers=self.headers)
        self.assertEqual(response.status_code,413)
        response=self.client.put('/api/v2/drafts/test/matches',json={'matches':[]})
        self.assertEqual(response.status_code,403)
    def test_compact_unknown_scene_still_rejected_by_business_validator(self):
        draft={'segments':[{'segment_id':'s1'}], 'clips':[], 'asset_ids':[]}
        actions=object.__new__(V2Actions)
        actions.store=SimpleNamespace(asset=lambda aid:None)
        with patch.object(self.studio,'replace_matches_v2',side_effect=lambda identifier,rows:
                          actions._validate_matches(draft,rows,allow_incomplete=True,candidate_limit=4500)):
            response=self.put({'clip_table':['not-registered'],'matches':[{'segment_id':'s1','clip_indices':[0]}]})
        self.assertEqual(response.status_code,400,response.json)
        self.assertIn('镜头无效',response.json['error'])

@unittest.skipUnless(os.name=='nt','DPAPI is Windows-specific')
class CredentialRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.settings=Settings(self.root)
        self.studio=FakeStudio();self.studio.settings=self.settings
        self.app=create_app(self.studio,token='review-only-token');self.client=self.app.test_client()
        self.headers={'X-Local-Token':'review-only-token'}
        self.bad=b'damaged-encrypted-audit-fixture';self.settings.secret_path.write_bytes(self.bad)
    def reset(self,confirm=True):
        return self.client.post('/api/settings/reset-credentials',json={'confirm':confirm},headers=self.headers)
    def test_corruption_is_public_boolean_not_content(self):
        public=self.settings.public()
        self.assertIs(public.get('credentials_need_reset'),True)
        self.assertFalse(public['glm_key_set'])
        self.assertNotIn(self.bad.decode(),json.dumps(public))
    def test_no_confirmation_does_not_touch_corrupt_file(self):
        for confirmation in [None,False,1,'true']:
            self.assertEqual(self.reset(confirmation).status_code,400)
        self.assertEqual(self.settings.secret_path.read_bytes(),self.bad)
        self.assertEqual(list(self.root.glob('secrets.unreadable.*.dpapi')),[])
    def test_confirmed_reset_archives_original_then_allows_new_key(self):
        self.settings.update({'bgm_gain':.3})
        old_preferences=self.settings.path.read_bytes()
        response=self.reset()
        self.assertEqual(response.status_code,200,response.json)
        backups=list(self.root.glob('secrets.unreadable.*.dpapi'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),self.bad)
        self.assertEqual(self.settings.path.read_bytes(),old_preferences)
        self.assertFalse(response.json['data']['credentials_need_reset'])
        self.assertFalse(response.json['data']['glm_key_set'])
        self.assertEqual(self.settings.secret('glm'),'')
        self.settings.update({'glm_api_key':'synthetic-not-a-real-key'})
        self.assertEqual(self.settings.secret('glm'),'synthetic-not-a-real-key')
        self.assertNotIn('synthetic-not-a-real-key',json.dumps(self.settings.public()))
        self.assertEqual(Settings(self.root).secret('glm'),'synthetic-not-a-real-key')
    def test_reset_healthy_or_missing_credentials_is_refused(self):
        self.settings.secret_path.unlink()
        self.assertEqual(self.reset().status_code,400)
        self.settings.update({'fish_api_key':'synthetic-fish-key'})
        raw=self.settings.secret_path.read_bytes()
        self.assertEqual(self.reset().status_code,400)
        self.assertEqual(self.settings.secret_path.read_bytes(),raw)
        self.assertEqual(self.settings.secret('fish'),'synthetic-fish-key')
    def test_archive_failure_preserves_old_credentials(self):
        original=Path.open
        def fail_archive(path,*args,**kwargs):
            if path.name.startswith('secrets.unreadable.'):raise OSError('injected archive failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',fail_archive): response=self.reset()
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.settings.secret_path.read_bytes(),self.bad)
    def test_reset_replace_failure_preserves_old_and_backup(self):
        with patch('mixstudio.settings.os.replace',side_effect=OSError('injected replace failure')):
            response=self.reset()
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.settings.secret_path.read_bytes(),self.bad)
        self.assertEqual(len(list(self.root.glob('secrets.unreadable.*.dpapi'))),1)
        self.assertFalse(list(self.root.glob('*.reset-*.tmp')))
    def test_reset_encryption_failure_never_replaces_or_archives(self):
        with patch('mixstudio.settings._dpapi',side_effect=ValueError('injected encryption failure')):
            response=self.reset()
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.settings.secret_path.read_bytes(),self.bad)
        self.assertEqual(list(self.root.glob('secrets.unreadable.*.dpapi')),[])
    def test_wrong_decrypted_structure_can_be_reset(self):
        for value in [[],None,{'glm':42}]:
            with self.subTest(value=value):
                self.settings.secret_path.write_bytes(base64.b64encode(_dpapi(json.dumps(value).encode())))
                self.assertTrue(self.settings.public()['credentials_need_reset'])
                self.assertEqual(self.reset().status_code,200)
    def test_reset_requires_authenticated_local_session(self):
        for headers in [{},{**self.headers,'Origin':'https://other.invalid'}]:
            response=self.client.post('/api/settings/reset-credentials',headers=headers,json={'confirm':True})
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.settings.secret_path.read_bytes(),self.bad)

if __name__=='__main__':unittest.main()

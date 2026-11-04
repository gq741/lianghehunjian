import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile

from mixstudio import updates
from mixstudio.desktop import DialogBridge

SPEC=importlib.util.spec_from_file_location('tested_updater',Path(__file__).resolve().parents[1]/'tools/apply_update.py')
up=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(up)


def row(name,data):
    return {'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}


class UpdateFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'安装 软件';self.root.mkdir()
        self.bundle=Path(self.temp.name)/'更新包';self.bundle.mkdir()
        self.before={'app/mixstudio/sample.py':b'VALUE = 1\n',
                     'RELEASE.json':b'{"date":"2026-10-29"}',
                     'runtime/python.exe':b'placeholder-not-executed',
                     'app/tools/portable_check.py':b'# fixture\n'}
        for name,data in self.before.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.before['FILES.sha256.json']=json.dumps([row(n,d) for n,d in self.before.items()]).encode()
        (self.root/'FILES.sha256.json').write_bytes(self.before['FILES.sha256.json'])
        new={'app/mixstudio/sample.py':b'VALUE = 2\n','app/mixstudio/added.py':b'NEW = True\n',
             'RELEASE.json':b'{"date":"2026-11-04", "version":"2026.11.04"}'}
        files={**self.before,**new};files.pop('FILES.sha256.json')
        new['FILES.sha256.json']=json.dumps([row(n,d) for n,d in files.items()]).encode()
        for name,data in new.items():
            p=self.bundle/'payload'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.manifest={'format':1,'version':'2026.11.04','bases':{
            up.sha(self.root/'FILES.sha256.json'):{'version':'2026.10.29',
                'old':{n:hashlib.sha256(self.before[n]).hexdigest() if n in self.before else None for n in new}}},
                'files':[row(n,d) for n,d in new.items()]}
        self.save()
        self.personal=Path(self.temp.name)/'个人数据';self.personal.mkdir()
        self.sentinel=b'DPAPI-ciphertext-fixture; music/tasks/cache untouched'
        (self.personal/'credentials.dat').write_bytes(self.sentinel)

    def save(self):
        (self.bundle/'UPDATE.json').write_text(json.dumps(self.manifest),'utf8')

    def apply(self,**kwargs):
        with patch.object(up,'running_processes',return_value=[]):
            return up.apply(self.root,self.bundle,checker=lambda *a:{'exit_status':0},**kwargs)

    def assert_original(self):
        for n,b in self.before.items():self.assertEqual((self.root/n).read_bytes(),b)
        self.assertFalse((self.root/'app/mixstudio/added.py').exists())

    def test_update_backup_and_personal_data_untouched(self):
        result=self.apply();self.assertEqual(result['status'],'updated')
        self.assertEqual((self.root/'app/mixstudio/sample.py').read_bytes(),b'VALUE = 2\n')
        self.assertEqual((self.personal/'credentials.dat').read_bytes(),self.sentinel)
        self.assertFalse((self.root/up.GATE).exists())

    def test_rollback_restores_exact_bytes_and_new_file_absence(self):
        result=self.apply();r=up.restore(self.root,Path(result['backup']))
        self.assertEqual(r['status'],'rolled_back');self.assert_original()

    def test_idempotent_already_current(self):
        self.apply();self.assertEqual(self.apply()['status'],'already_current')

    def test_corrupt_payload_does_not_touch_program(self):
        (self.bundle/'payload/app/mixstudio/sample.py').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'校验失败'):self.apply()
        self.assert_original()

    def test_wrong_version_does_not_touch_program(self):
        self.manifest['bases']={};self.save()
        with self.assertRaisesRegex(ValueError,'版本不匹配'):self.apply()
        self.assert_original()

    def test_user_modified_program_is_not_overwritten(self):
        p=self.root/'app/mixstudio/sample.py';p.write_bytes(b'user edits')
        with self.assertRaisesRegex(ValueError,'已变化'):self.apply()
        self.assertEqual(p.read_bytes(),b'user edits')

    def test_running_app_blocks_before_backup(self):
        with patch.object(up,'running_processes',return_value=[123]):
            with self.assertRaisesRegex(ValueError,'退出'):up.apply(self.root,self.bundle)
        self.assert_original();self.assertFalse((self.root/up.GATE).exists())

    def test_selfcheck_failure_automatically_restores(self):
        with patch.object(up,'running_processes',return_value=[]):
            with self.assertRaisesRegex(ValueError,'fixture'):
                up.apply(self.root,self.bundle,checker=Mock(side_effect=ValueError('fixture')))
        self.assert_original();self.assertFalse((self.root/up.GATE).exists())

    def test_write_failure_restores_prior_replacements(self):
        original=up.replace_file; calls=[0]
        def fail_once(src,dst):
            calls[0]+=1
            if calls[0]==2:raise OSError('fixture write failure')
            return original(src,dst)
        with patch.object(up,'replace_file',side_effect=fail_once):
            with self.assertRaises(OSError):self.apply()
        self.assert_original()

    def test_crash_journal_is_recovered_next_run(self):
        with patch.object(up,'running_processes',return_value=[]):
            with self.assertRaises(KeyboardInterrupt):
                up.apply(self.root,self.bundle,checker=Mock(side_effect=KeyboardInterrupt))
        self.assertTrue((self.root/up.GATE).exists())
        result=self.apply();self.assertEqual(result['status'],'rolled_back');self.assert_original()

    def test_rollback_preserves_later_edits(self):
        result=self.apply();p=self.root/'app/mixstudio/sample.py';p.write_bytes(b'later work')
        with self.assertRaisesRegex(ValueError,'再次变化'):up.restore(self.root,Path(result['backup']))
        self.assertEqual(p.read_bytes(),b'later work')

    def test_traversal_windows_aliases_and_personal_data_rejected(self):
        for name in ('../keys','/absolute','a//b','a\\b','C:/x','a:b','a./b','nul.txt','a/CON'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):up.clean_path(self.root,name)
        for name in ('settings.json','data/credentials.dat','runtime/python.exe','models/model.bin'):
            self.assertFalse(up.allowed(name))

    def test_duplicate_casefold_file_rejected(self):
        self.manifest['files'].append({**self.manifest['files'][0],'path':'app/mixstudio/SAMPLE.py'})
        self.save()
        with self.assertRaisesRegex(ValueError,'重复'):self.apply()

    def test_missing_base_hash_rejected(self):
        for base in self.manifest['bases'].values():base['old'].pop('RELEASE.json')
        self.save()
        with self.assertRaisesRegex(ValueError,'旧版本校验'):self.apply()

    def test_manifest_mismatch_rejected(self):
        p=self.bundle/'payload/FILES.sha256.json';p.write_text('[]','utf8')
        self.manifest['files']=[row(x['path'],p.read_bytes()) if x['path']=='FILES.sha256.json' else x for x in self.manifest['files']]
        self.save()
        with self.assertRaisesRegex(ValueError,'总清单'):self.apply()


class NetworkUpdates(unittest.TestCase):
    def test_only_trusted_https_repo_and_asset_redirects(self):
        self.assertTrue(updates.trusted_url(updates.API))
        for url in ('http://github.com/gq741/lianghehunjian/releases/download/x/y',
                    'https://github.com/evil/repo/releases/download/x/y','https://127.0.0.1/file',
                    'https://user:pass@github.com/gq741/lianghehunjian/releases/download/x/y'):
            self.assertFalse(updates.trusted_url(url))
        self.assertTrue(updates.trusted_url('https://release-assets.githubusercontent.com/x',redirect=True))

    def test_no_background_network_on_session_or_status(self):
        with patch.object(updates,'fetch') as f:
            session=updates.UpdateSession();session.status();f.assert_not_called()

    def test_empty_release_list_means_no_update(self):
        with patch.object(updates,'fetch',return_value=b'[]'):
            self.assertFalse(updates.check(None)['available'])

    def test_fetch_http_failure_has_actionable_fixed_message(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=OSError('secret body')):
            with self.assertRaisesRegex(ValueError,'GitHub') as ctx:updates.fetch(updates.API,10)
        self.assertNotIn('secret',str(ctx.exception))

    def test_release_digest_and_version_checked(self):
        release=[{'tag_name':'v2099.01.01','assets':[{'name':'UPDATE_FEED.json',
                  'browser_download_url':'https://github.com/gq741/lianghehunjian/releases/download/v2099.01.01/UPDATE_FEED.json',
                  'digest':'sha256:bad'}]}]
        with patch.object(updates,'fetch',side_effect=[json.dumps(release).encode(),b'{}']):
            with self.assertRaisesRegex(ValueError,'校验失败'):updates.check(None)

    def test_archive_corruption_before_extraction(self):
        with tempfile.TemporaryDirectory() as d,patch.object(updates,'fetch',return_value=b'bad'):
            with self.assertRaisesRegex(ValueError,'校验失败'):
                updates.download({'archive':{'url':updates.API,'bytes':3,'sha256':'0'*64}},Path(d))
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_zip_slip_duplicate_and_symlink_rejected(self):
        for name in ('../bad.py','/bad.py','a\\b','CON'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as d:
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w') as z:z.writestr(name,b'bad')
                data=stream.getvalue();offer={'archive':{'url':updates.API,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
                with patch.object(updates,'fetch',return_value=data):
                    with self.assertRaisesRegex(ValueError,'路径|入口'):updates.download(offer,Path(d))

    def test_busy_native_app_not_closed(self):
        bridge=DialogBridge();bridge._studio=Mock();bridge._updates=Mock();bridge._window=Mock()
        import threading
        bridge._studio.action_lock=threading.RLock()
        bridge._studio.store.active_jobs.return_value=[{'id':'working'}]
        self.assertFalse(bridge.install_update()['ok']);bridge._window.destroy.assert_not_called()

    def test_launch_failure_does_not_leave_app_fenced(self):
        bridge=DialogBridge();bridge._studio=Mock();bridge._updates=Mock();bridge._window=Mock()
        import threading
        bridge._studio.action_lock=threading.RLock();bridge._studio.storage_cleaning=False
        bridge._studio.store.active_jobs.return_value=[];bridge._updates.launch.side_effect=ValueError('fixture')
        self.assertFalse(bridge.install_update()['ok']);self.assertFalse(bridge._studio.update_pending)
        bridge._window.destroy.assert_not_called()

    def test_ui_dirty_guards_and_no_automatic_update(self):
        js=(Path(__file__).resolve().parents[1]/'web/app.js').read_text('utf8')
        self.assertIn('state.settingsDirty || state.draftDirty || state.matchesDirty',js)
        self.assertIn('退出并安装更新',js)
        self.assertEqual(js.count('api("/api/updates/check"'),1)


if __name__=='__main__':unittest.main()

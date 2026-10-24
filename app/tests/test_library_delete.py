"""Only disposable fixtures are moved; no real user material is deleted."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mixstudio.service import Studio, fingerprint
from mixstudio.http_app import create_app


class LibraryDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.studio=Studio(self.root/'runtime')
        self.trash=self.root/'recycle';self.trash.mkdir()
        for name,library in [('a','local'),('b','local'),('c','douyin')]:
            path=self.root/(name+'.mp4');path.write_bytes(name.encode())
            self.studio.store.put_asset({'id':name,'name':path.name,'path':str(path),
                'sha256':fingerprint(path),'libraries':[library],'duration':3.,'clips':[],
                'imported_at':1,'availability':'available','selectable':True})
        self.stub=patch('mixstudio.recycle.recycle_file',side_effect=lambda p:Path(p).rename(self.trash/Path(p).name))
        self.recycle=self.stub.start()

    def tearDown(self):
        self.stub.stop();self.studio.close();self.temp.cleanup()

    def test_confirmation_is_required_and_empty_or_wrong_library_rejected(self):
        for ids,library,confirm in [(['a'],'local',False),([], 'local',True),(['a'],[],True),(['a'],'douyin',True),([True],'local',True)]:
            with self.assertRaises(ValueError):self.studio.remove_library_assets(ids,library,confirm)
        self.recycle.assert_not_called();self.assertTrue((self.root/'a.mp4').exists())

    def test_only_selected_files_recycled_records_retained_and_repeat_safe(self):
        result=self.studio.remove_library_assets(['a','b','a'],'local',True)
        self.assertEqual(result['removed_count'],2)
        self.assertEqual(set(result['removed_ids']),{'a','b'})
        self.assertFalse((self.root/'a.mp4').exists());self.assertTrue((self.root/'c.mp4').exists())
        self.assertEqual(self.studio.store.asset('a')['availability'],'trashed')
        self.assertFalse(self.studio.store.asset('a')['selectable'])
        again=self.studio.remove_library_assets(['a'],'local',True)
        self.assertEqual(again['removed_count'],0);self.assertEqual(len(again['failed']),1)
        self.assertEqual(self.recycle.call_count,2)

    def test_bad_batch_id_is_rejected_before_first_file_move(self):
        with self.assertRaises(ValueError):self.studio.remove_library_assets(['a','missing'],'local',True)
        self.recycle.assert_not_called();self.assertTrue((self.root/'a.mp4').exists())

    def test_shared_path_retired_in_both_libraries_but_copy_preserved(self):
        source=self.studio.store.asset('a')
        self.studio.store.put_asset({**source,'id':'alias','libraries':['douyin']})
        result=self.studio.remove_library_assets(['a'],'local',True)
        self.assertEqual(set(result['removed_ids']),{'a','alias'})
        self.assertEqual(self.recycle.call_count,1)
        self.assertFalse(self.studio.store.asset('alias')['selectable'])
        self.assertTrue((self.root/'c.mp4').exists())

    def test_replaced_source_is_not_recycled(self):
        (self.root/'a.mp4').write_bytes(b'new unrelated content')
        result=self.studio.remove_library_assets(['a'],'local',True)
        self.assertEqual(result['removed_count'],0);self.assertEqual(len(result['failed']),1)
        self.recycle.assert_not_called();self.assertTrue((self.root/'a.mp4').exists())

    def test_partial_failure_keeps_failed_item_selectable(self):
        def recycle(path):
            if path.name=='b.mp4':raise OSError('文件正在使用')
            path.rename(self.trash/path.name)
        self.recycle.side_effect=recycle
        result=self.studio.remove_library_assets(['a','b'],'local',True)
        self.assertEqual(result['removed_count'],1)
        self.assertEqual(result['failed'][0]['asset_ids'],['b'])
        self.assertTrue(self.studio.store.asset('b')['selectable'])
        self.assertTrue((self.root/'b.mp4').exists())

    def test_active_draft_and_scan_jobs_prevent_recycling(self):
        self.studio.store.put_draft({'id':'draft','asset_ids':['a'],'created_at':1})
        self.studio.store.create_job('running','render',{'draft_id':'draft'})
        with self.assertRaises(ValueError):self.studio.remove_library_assets(['a'],'local',True)
        self.studio.store.update_job('running',status='completed')
        self.studio.store.create_job('scan','scan',{'folder':str(self.root)})
        with self.assertRaises(ValueError):self.studio.remove_library_assets(['a'],'local',True)
        self.recycle.assert_not_called()

    def test_metadata_failure_reports_file_already_recycled(self):
        with patch.object(self.studio.store,'mark_assets_trashed',side_effect=sqlite3.OperationalError('injected')):
            result=self.studio.remove_library_assets(['a'],'local',True)
        self.assertEqual(result['removed_ids'],['a']);self.assertTrue(result['warnings'])
        self.assertFalse((self.root/'a.mp4').exists())

    def test_reimport_reactivates_restored_original(self):
        self.studio.remove_library_assets(['a'],'local',True)
        (self.trash/'a.mp4').rename(self.root/'a.mp4')
        restored=self.studio.store.activate_asset({'id':'a'})
        self.assertTrue(restored['selectable']);self.assertEqual(restored['availability'],'available')

    def test_local_http_requires_auth_and_explicit_confirmation(self):
        app=create_app(self.studio);client=app.test_client();route='/api/library/remove'
        payload={'asset_ids':['a'],'library':'local','confirmed':True}
        self.assertEqual(client.post(route,json=payload).status_code,403)
        headers={'X-Local-Token':app.config['LOCAL_TOKEN']}
        self.assertEqual(client.post(route,json={**payload,'confirmed':False},headers=headers).status_code,400)
        response=client.post(route,json=payload,headers=headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['data']['destination'],'recycle_bin')

    def test_recycling_guard_blocks_new_v2_work(self):
        self.studio._recycling_asset_ids={'a'}
        with self.assertRaises(ValueError):self.studio._v2_assets(['a'])

    def test_native_adapter_has_recycle_only_flags_and_no_unlink(self):
        from mixstudio import recycle
        self.assertTrue(recycle.RECYCLE_FLAGS & 0x00080000)
        self.assertTrue(recycle.RECYCLE_FLAGS & 0x00100000)
        source=Path(recycle.__file__).read_text('utf-8')
        self.assertNotIn('.unlink(',source);self.assertNotIn('rmtree(',source)


if __name__=='__main__':unittest.main()

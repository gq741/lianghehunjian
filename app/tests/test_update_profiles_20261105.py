import hashlib
from test_updates import UpdateFixture, up


class ProfileUpdateTests(UpdateFixture):
    def add_profile(self):
        (self.root/'量禾混剪.exe').write_bytes(b'official-launcher-fixture')
        paths=['app/mixstudio/sample.py', 'app/tools/portable_check.py',
               'runtime/python.exe', '量禾混剪.exe']
        self.manifest['compatibility_profiles']=[{'version':'known-mini',
            'hashes':{p:up.sha(self.root/p) for p in paths}}]
        self.manifest['bases']={}
        self.save()

    def test_metadata_variant_updates_and_rolls_back(self):
        self.add_profile()
        changed=self.root/'FILES.sha256.json'
        changed.write_text(changed.read_text()+'\n  ','utf8')
        before=changed.read_bytes()
        result=self.apply()
        self.assertEqual(result['status'],'updated')
        up.restore(self.root, __import__('pathlib').Path(result['backup']))
        self.assertEqual(changed.read_bytes(),before)
        self.assertEqual((self.root/'app/mixstudio/sample.py').read_bytes(),b'VALUE = 1\n')

    def test_profile_rejects_unknown_source_edit(self):
        self.add_profile()
        (self.root/'app/mixstudio/sample.py').write_bytes(b'UNKNOWN = 2\n')
        with self.assertRaisesRegex(ValueError,'版本不匹配'):
            self.apply()
        self.assertEqual((self.root/'app/mixstudio/sample.py').read_bytes(),b'UNKNOWN = 2\n')

    def test_profile_rejects_extra_runtime_module(self):
        self.add_profile()
        (self.root/'app/mixstudio/injected.py').write_bytes(b'EXTRA = True\n')
        with self.assertRaisesRegex(ValueError,'版本不匹配'):
            self.apply()

    def test_profile_rejects_changed_launcher(self):
        self.add_profile()
        (self.root/'量禾混剪.exe').write_bytes(b'unknown-launcher')
        with self.assertRaisesRegex(ValueError,'版本不匹配'):
            self.apply()

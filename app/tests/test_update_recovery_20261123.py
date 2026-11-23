"""Offline program-update invariants; no real installation or API calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_updates as fixture

up = fixture.up


class UpdateRecoveryTests(unittest.TestCase):
    setUp = fixture.UpdateFixture.setUp
    apply = fixture.UpdateFixture.apply
    save = fixture.UpdateFixture.save
    assert_original = fixture.UpdateFixture.assert_original

    def test_interrupted_manual_rollback_stays_gated_and_resumes(self):
        backup = Path(self.apply()['backup'])
        original = up.replace_file
        count = 0
        def interrupt(source, dest):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt('interrupt a partially restored copy')
            return original(source, dest)
        with patch.object(up, 'replace_file', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            up.restore(self.root, backup)
        self.assertTrue((self.root/up.GATE).is_file())
        self.assertEqual(json.loads((backup/'journal.json').read_text('utf8'))['status'], 'rolling_back')
        self.assertEqual(self.apply()['status'], 'rolled_back')
        self.assertFalse((self.root/up.GATE).exists())
        self.assert_original()

    def test_backup_abandoned_before_journal_does_not_block_previous_rollback(self):
        self.apply()
        # Process interruption while making backup copies, before any writes to app.
        orphan = self.root/up.BACKUPS/'20990101-130000-f00dfa11'
        orphan.mkdir()
        with patch.object(up.sys, 'executable', str(self.root/'runtime/python.exe')), patch.object(up, 'running_processes', return_value=[]):
            self.assertEqual(up.main(['--target', str(self.root), '--rollback']), 0)
        self.assert_original()
        self.assertTrue(orphan.exists())

    def test_current_receipt_does_not_mask_missing_unchanged_program_file(self):
        self.apply()
        p = self.root/'app/tools/portable_check.py'
        p.write_bytes(b'# changed locally after upgrade\n')
        with self.assertRaisesRegex(ValueError, '校验'):
            self.apply()
        self.assertEqual(p.read_bytes(), b'# changed locally after upgrade\n')

    def test_profile_checks_unchanged_application_entrypoint(self):
        # An old source-sync profile omits app/app.py; this file is not shipped
        # by this patch and must still match the destination program manifest.
        path = 'app/app.py'
        p = self.root/path
        p.write_bytes(b'# old local edit\n')
        launcher = self.root/'量禾混剪.exe'; launcher.write_bytes(b'fixture')
        names = ['runtime/python.exe','量禾混剪.exe','app/tools/portable_check.py','app/mixstudio/sample.py']
        self.manifest['compatibility_profiles'] = [{'hashes': {n:up.sha(self.root/n) for n in names}}]
        self.manifest['bases'] = {}
        f = self.bundle/'payload/FILES.sha256.json'
        rows = json.loads(f.read_bytes()); rows.append(fixture.row(path, b'# expected entry\n'))
        f.write_text(json.dumps(rows),'utf8')
        self.manifest['files'] = [fixture.row('FILES.sha256.json',f.read_bytes()) if r['path']=='FILES.sha256.json' else r for r in self.manifest['files']]
        self.save()
        with self.assertRaisesRegex(ValueError, '校验'):
            self.apply()
        self.assert_original()
        self.assertEqual(p.read_bytes(), b'# old local edit\n')

    def test_known_sourcesync_base_with_stale_manifest_uses_verified_profile(self):
        # 11.05 explicitly lists the WorkBuddy-synced program bytes as the
        # old patch inputs but FILES still describes the earlier .py files.
        source = self.root/'app/mixstudio/sample.py'
        synced = b'VALUE = 11  # previous official source sync\n'; source.write_bytes(synced)
        (self.root/'量禾混剪.exe').write_bytes(b'fixture')
        names = ['runtime/python.exe','量禾混剪.exe','app/tools/portable_check.py','app/mixstudio/sample.py']
        self.manifest['compatibility_profiles'] = [{'hashes': {n:up.sha(self.root/n) for n in names}}]
        key = up.sha(self.root/'FILES.sha256.json')
        self.manifest['bases'][key]['old']['app/mixstudio/sample.py'] = up.sha(source)
        self.save()
        result = self.apply(); self.assertEqual(result['status'], 'updated')
        self.assertEqual(up.restore(self.root, Path(result['backup']))['status'], 'rolled_back')
        self.assertEqual(source.read_bytes(), synced)

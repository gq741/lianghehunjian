"""Real Windows PowerShell 5.1 restore acceptance using tiny unsigned fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'Restore-Release.ps1'
POWERSHELL = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
RECORDS = []


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='还原 fixtures 中文 空格-')
        self.base = Path(self.temp.name)
        self.download = self.base / '下载 分块'
        self.parent = self.base / '还原 目标'
        self.download.mkdir()
        self.parent.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, extras=(), modify_manifest=None, omit_manifest=False):
        files = {'软件说明.txt': '量禾混剪，可读中文。'.encode(), 'app/main.py': b'print("fixture")\n'}
        manifest = [{'path': path, 'bytes': len(data), 'sha256': sha(data)} for path, data in files.items()]
        if modify_manifest:
            modify_manifest(manifest)
        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
        archive_path = self.base / 'test.zip'
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr('LiangHeMix/', b'')
            for path, data in files.items():
                z.writestr('LiangHeMix/' + path, data)
            if not omit_manifest:
                z.writestr('LiangHeMix/FILES.sha256.json', manifest_bytes)
            for name, data in extras:
                z.writestr(name, data)
        data = archive_path.read_bytes()
        parts = []
        for index, start in enumerate(range(0, len(data), 500), 1):
            piece = data[start:start + 500]
            name = f'LiangHeMix-20261024.zip.part{index:02}'
            (self.download / name).write_bytes(piece)
            parts.append({'name': name, 'bytes': len(piece), 'sha256': sha(piece)})
        info = dict(schema=1, product='量禾混剪', version='fixture', archive_name='LiangHeMix-20261024.zip',
                    root_dir='LiangHeMix', archive_bytes=len(data), archive_sha256=sha(data),
                    manifest_sha256=sha(manifest_bytes), parts=parts)
        self.write_parts(info)
        return info

    def write_parts(self, info):
        (self.download / 'PARTS.json').write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')

    def run_restore(self, code=1, run_check=False):
        command = [str(POWERSHELL), '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(SCRIPT), '-DownloadDir', str(self.download), '-DestinationParent', str(self.parent)]
        if run_check:
            command.append('-RunCheck')
        before = set(self.parent.glob('LiangHeMix-restore-report-*.json'))
        result = subprocess.run(command, capture_output=True, timeout=60)
        # Windows PowerShell console encoding can differ from file encoding; JSON evidence is UTF-8.
        output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        reports = list(set(self.parent.glob('LiangHeMix-restore-report-*.json')) - before)
        report = json.loads(reports[-1].read_text(encoding='utf-8-sig')) if reports else None
        RECORDS.append(dict(test=self.id(), command=command, exit_code=result.returncode, output=output, report=report))
        self.assertEqual(code, result.returncode, output)
        self.assertIsNotNone(report, output)
        self.assertEqual(code, report['exit_code'])
        return report

    def test_valid_unicode_space(self):
        self.fixture()
        report = self.run_restore(0)
        self.assertEqual('PASS', report['restore_status'])
        self.assertEqual('NOT_RUN', report['environment_status'])
        self.assertEqual(2, report['checked_files'])
        self.assertEqual('量禾混剪，可读中文。', (self.parent / 'LiangHeMix/软件说明.txt').read_text())
        self.assertFalse(list(self.parent.glob('.LiangHeMix-*.zip')))

    def test_volume_root_normalization_without_writing_root(self):
        # Load only the pure helper, not script main, so no unknown files are created at E:\.
        command_text = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Parse failure' }
$function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-NormalizedDirectory' }, $true)
if ($null -eq $function) { throw 'Missing pure normalization helper' }
. ([ScriptBlock]::Create($function.Extent.Text))
$root = [IO.Path]::GetPathRoot($args[0])
$actual = Get-NormalizedDirectory $root
if ($actual -cne $root) { throw "Root changed: $root => $actual" }
if ((Join-Path $actual 'LiangHeMix') -cne ($root + 'LiangHeMix')) { throw 'Drive-relative target detected' }
$nested = Get-NormalizedDirectory ($args[1] + '\')
if ($nested -cne $args[1]) { throw 'Nested trailing slash was not removed' }
Write-Output ('VOLUME_ROOT_NORMALIZATION=PASS; root=' + $actual + '; no root writes')
'''
        # -Command arguments are not reliably passed through $args on PS5; invoke a temporary fixture script.
        helper = self.base / 'volume-root-check.ps1'
        helper.write_text(command_text, encoding='utf-8-sig')
        command = [str(POWERSHELL), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(helper), str(SCRIPT), str(self.parent)]
        result = subprocess.run(command, capture_output=True, timeout=60)
        output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        RECORDS.append(dict(test=self.id(), command=command, exit_code=result.returncode, output=output))
        self.assertEqual(0, result.returncode, output)
        self.assertIn('VOLUME_ROOT_NORMALIZATION=PASS', output)

    def test_missing_part(self):
        info = self.fixture()
        (self.download / info['parts'][0]['name']).unlink()
        self.assertEqual('verify_parts', self.run_restore()['stage'])
        self.assertFalse((self.parent / 'LiangHeMix').exists())

    def test_bad_part_hash(self):
        info = self.fixture()
        info['parts'][0]['sha256'] = '0' * 64
        self.write_parts(info)
        self.assertEqual('verify_parts', self.run_restore()['stage'])

    def test_bad_archive_hash(self):
        info = self.fixture()
        info['archive_sha256'] = '0' * 64
        self.write_parts(info)
        self.assertEqual('merge', self.run_restore()['stage'])

    def test_existing_target_is_preserved(self):
        self.fixture()
        existing = self.parent / 'LiangHeMix'
        existing.mkdir()
        sentinel = existing / '原文件.txt'
        sentinel.write_bytes(b'unchanged')
        self.assertEqual('input', self.run_restore()['stage'])
        self.assertEqual(b'unchanged', sentinel.read_bytes())

    def test_part_order(self):
        info = self.fixture()
        info['parts'].reverse()
        self.write_parts(info)
        self.assertEqual('verify_parts', self.run_restore()['stage'])

    def test_manifest_hash(self):
        info = self.fixture()
        info['manifest_sha256'] = '0' * 64
        self.write_parts(info)
        self.assertEqual('verify_files', self.run_restore()['stage'])

    def test_manifest_entry_hash(self):
        self.fixture(modify_manifest=lambda m: m[0].update(sha256='0' * 64))
        self.assertEqual('verify_files', self.run_restore()['stage'])

    def test_missing_manifest(self):
        self.fixture(omit_manifest=True)
        self.assertEqual('inspect_zip', self.run_restore()['stage'])

    def test_extra_immutable_file(self):
        self.fixture(extras=[('LiangHeMix/extra.exe', b'extra')])
        self.assertEqual('verify_files', self.run_restore()['stage'])

    def test_no_selfcheck_entry_is_not_success(self):
        self.fixture()
        report = self.run_restore(2, run_check=True)
        self.assertEqual('PASS', report['restore_status'])
        self.assertEqual('NEEDS_ATTENTION', report['environment_status'])
        self.assertTrue((self.parent / 'LiangHeMix').is_dir())

    def test_paths_and_links_rejected_before_extract(self):
        cases = ['../escape.txt', '/escape.txt', 'C:/escape.txt', 'LiangHeMix/../escape.txt',
                 'LiangHeMix/a:stream', 'LiangHeMix/NUL.txt', 'LiangHeMix/dot./x',
                 'LiangHeMix/app/MAIN.py', 'LiangHeMix/app/main.py/child', 'LiangHeMix\\..\\escape.txt']
        symlink = zipfile.ZipInfo('LiangHeMix/link')
        symlink.create_system = 3
        symlink.external_attr = (0o120777 << 16)
        cases.append(symlink)
        reparse = zipfile.ZipInfo('LiangHeMix/reparse')
        reparse.external_attr = 0x400
        cases.append(reparse)
        for index, path in enumerate(cases):
            with self.subTest(path=str(path)):
                self.fixture(extras=[(path, b'bad')])
                report = self.run_restore()
                self.assertEqual('inspect_zip', report['stage'])
                self.assertFalse((self.parent / 'LiangHeMix').exists())
                self.assertFalse(list(self.parent.glob('.LiangHeMix-stage-*')))


if __name__ == '__main__':
    ROOT.joinpath('tests').mkdir(exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RestoreTests)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = ROOT / 'evidence/restore-fixtures.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(dict(tests=outcome.testsRun, failures=len(outcome.failures),
                          errors=len(outcome.errors), records=RECORDS), ensure_ascii=False, indent=2), encoding='utf-8')
    raise SystemExit(not outcome.wasSuccessful())

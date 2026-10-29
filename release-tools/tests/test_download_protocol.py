"""Real PowerShell 5.1 fixtures; public network functions stubbed, filesystem/hash/child PS real."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'Download-Release.ps1'
PS = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
RECORDS = []
NAMES = ['PARTS.json', 'Restore-Release.ps1', 'Restore-Release.cmd', 'WORKBUDDY.md', 'START_HERE.txt',
         'LICENSE.txt', 'ReleaseNotes.md', 'SHA256SUMS.txt']


def sha(data):
    return hashlib.sha256(data).hexdigest()


WRAPPER = r'''
param([string]$SourceScript,[string]$FixtureRoot,[string]$Directory,[string]$Mode,[switch]$Only)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$t=$null;$e=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($SourceScript,[ref]$t,[ref]$e)
if($e.Count){throw ($e|Out-String)}
$functions=$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)
foreach($f in $functions){. ([ScriptBlock]::Create($f.Extent.Text))}
$script:FixtureRoot=$FixtureRoot
$script:Mode=$Mode
$script:Counts=@{}
function Get-ReleaseMetadata {
    return Get-Content -LiteralPath (Join-Path $script:FixtureRoot 'release.json') -Raw -Encoding UTF8|ConvertFrom-Json
}
function Receive-AssetBytes([string]$Uri,[string]$Path) {
    $name=[Uri]::UnescapeDataString(([Uri]$Uri).Segments[-1])
    if(-not $script:Counts.ContainsKey($name)){$script:Counts[$name]=0}
    $script:Counts[$name]++
    if($name -eq 'LiangHeMix-20261029.zip.part01') {
        if($script:Mode -eq 'failall'){throw [IO.IOException]::new('fixture transport unavailable')}
        if($script:Mode -eq 'retrytwice' -and $script:Counts[$name] -le 2){throw [IO.IOException]::new('fixture transient failure')}
        if($script:Mode -eq 'badfirst' -and $script:Counts[$name] -eq 1){[IO.File]::WriteAllBytes($Path,[byte[]]@(1,2));return}
    }
    [IO.File]::WriteAllBytes($Path,[IO.File]::ReadAllBytes((Join-Path $script:FixtureRoot $name)))
}
function Start-Sleep {param([int]$Seconds)}
$code=Invoke-DownloadRelease -Directory $Directory -Only ([bool]$Only)
$script:Counts|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $script:FixtureRoot 'receive-counts.json') -Encoding UTF8
exit [int]$code
'''


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='下载器 中文 fixtures-', dir=ROOT / 'tests')
        self.base = Path(self.temp.name)
        self.fixture = self.base / 'remote fixture'
        self.destination = self.base / '朋友 短目录'
        self.fixture.mkdir()
        self.destination.mkdir()
        self.wrapper = self.base / 'fixture-driver.ps1'
        self.wrapper.write_text(WRAPPER, encoding='utf-8-sig')
        self.release = self.make_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def make_fixture(self, restore_exit=0, part_count=37):
        files = {name: (name + ' fixture 中文\n').encode('utf-8') for name in NAMES}
        files['Restore-Release.ps1'] = ("param([string]$DownloadDir,[string]$DestinationParent,[switch]$RunCheck)\n"
            "if(-not $RunCheck){exit 9}\n"
            "'CALLED'|Set-Content -LiteralPath (Join-Path $PSScriptRoot 'restore-invoked.txt')\n"
            "Write-Output 'RESTORE_FIXTURE_CALLED'\n"
            f"exit {restore_exit}\n").encode('utf-8-sig')
        rows = []
        archive = b''
        for index in range(1, part_count + 1):
            data = ('tiny-part-' + str(index)).encode()
            name = f'LiangHeMix-20261029.zip.part{index:02}'
            files[name] = data
            rows.append(dict(name=name, bytes=len(data), sha256=sha(data)))
            archive += data
        info = dict(schema=1, product='量禾混剪', version='20261029', archive_name='LiangHeMix-20261029.zip',
                    root_dir='LiangHeMix', archive_bytes=len(archive), archive_sha256=sha(archive),
                    manifest_sha256='a' * 64, parts=rows)
        files['PARTS.json'] = json.dumps(info, ensure_ascii=False).encode()
        assets = []
        for name, data in files.items():
            (self.fixture / name).write_bytes(data)
            assets.append(dict(name=name, size=len(data), digest='sha256:' + sha(data), state='uploaded',
                browser_download_url='https://github.com/gq741/lianghehunjian/releases/download/v2026.10.29/' + name))
        # These public extras must neither overwrite the running entrypoint nor get downloaded.
        for name in ['Download-Release.ps1', 'Download-Release.cmd', 'Source code.zip', '../evil.txt']:
            assets.append(dict(name=name, size=1, digest=None, state='uploaded', browser_download_url='https://invalid.example/'))
        release = dict(tag_name='v2026.10.29', draft=False, assets=assets)
        self.save_release(release)
        return release

    def save_release(self, release):
        (self.fixture / 'release.json').write_text(json.dumps(release, ensure_ascii=False), encoding='utf-8')

    def run_download(self, expected=0, mode='ok', only=True):
        command = [str(PS), '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.wrapper),
                   '-SourceScript', str(SCRIPT), '-FixtureRoot', str(self.fixture), '-Directory', str(self.destination),
                   '-Mode', mode]
        if only:
            command.append('-Only')
        result = subprocess.run(command, capture_output=True, timeout=90)
        output = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')
        report_file = self.destination / 'download-report.json'
        report = json.loads(report_file.read_text(encoding='utf-8-sig')) if report_file.exists() else None
        counts_file = self.fixture / 'receive-counts.json'
        counts = json.loads(counts_file.read_text(encoding='utf-8-sig')) if counts_file.exists() else None
        RECORDS.append(dict(test=self.id(), command=command, exit_code=result.returncode, report=report,
                            receive_counts=counts, output=output))
        self.assertEqual(expected, result.returncode, output)
        self.assertIsNotNone(report, output)
        self.assertEqual(expected, report['exit_code'])
        return report, counts

    def test_37_parts_download_only_and_self_entry_not_overwritten(self):
        entry = self.destination / 'Download-Release.ps1'
        entry.write_bytes(b'friend entry untouched')
        report, counts = self.run_download()
        self.assertEqual('DOWNLOAD_ONLY_PASS', report['status'])
        self.assertEqual(45, len(report['assets']))
        self.assertEqual(45, len(counts))
        self.assertEqual(b'friend entry untouched', entry.read_bytes())
        self.assertFalse((self.destination / 'restore-invoked.txt').exists())
        self.assertFalse(list(self.destination.glob('*.partial')))

    def test_manifest_part_count_is_dynamic(self):
        for count in (1, 7, 43):
            with self.subTest(parts=count):
                self.make_fixture(part_count=count)
                target = self.base / ('dynamic-' + str(count))
                target.mkdir()
                self.destination = target
                report, counts = self.run_download()
                self.assertEqual('DOWNLOAD_ONLY_PASS', report['status'])
                self.assertEqual(len(NAMES) + count, len(report['assets']))
                self.assertEqual(len(NAMES) + count, len(counts))

    def test_existing_good_files_are_reused(self):
        self.run_download()
        report, counts = self.run_download()
        self.assertEqual({}, counts)
        self.assertTrue(all(row['result'] == 'REUSED' for row in report['assets']))

    def test_bad_existing_formal_file_is_never_overwritten(self):
        path = self.destination / 'WORKBUDDY.md'
        path.write_bytes(b'user copy must survive')
        report, counts = self.run_download(expected=1)
        self.assertEqual(b'user copy must survive', path.read_bytes())
        self.assertNotIn('WORKBUDDY.md', counts)
        self.assertIn('未覆盖', report['error'])

    def test_transient_failure_retries_at_most_three(self):
        report, counts = self.run_download(mode='retrytwice')
        self.assertEqual(3, counts['LiangHeMix-20261029.zip.part01'])
        self.assertEqual('DOWNLOAD_ONLY_PASS', report['status'])

    def test_partial_wrong_length_retried_and_never_published(self):
        report, counts = self.run_download(mode='badfirst')
        self.assertEqual(2, counts['LiangHeMix-20261029.zip.part01'])
        self.assertFalse(list(self.destination.glob('*.partial')))
        self.assertEqual('DOWNLOAD_ONLY_PASS', report['status'])

    def test_exhausted_transport_has_specific_missing_part(self):
        report, counts = self.run_download(expected=1, mode='failall')
        self.assertEqual(3, counts['LiangHeMix-20261029.zip.part01'])
        self.assertIn('LiangHeMix-20261029.zip.part01', report['error'])
        self.assertFalse((self.destination / 'LiangHeMix-20261029.zip.part01').exists())
        self.assertFalse(list(self.destination.glob('*.partial')))

    def test_restore_environment_exit_two_propagates(self):
        self.make_fixture(restore_exit=2)
        report, _ = self.run_download(expected=2, only=False)
        self.assertEqual('ENVIRONMENT_NEEDS_ATTENTION', report['status'])
        self.assertEqual(2, report['restore_exit_code'])
        self.assertTrue((self.destination / 'restore-invoked.txt').exists())

    def test_restore_success_propagates(self):
        report, _ = self.run_download(only=False)
        self.assertEqual('RESTORE_AND_ENVIRONMENT_PASS', report['status'])
        self.assertEqual(0, report['restore_exit_code'])

    def test_restore_failure_propagates(self):
        self.make_fixture(restore_exit=1)
        report, _ = self.run_download(expected=1, only=False)
        self.assertEqual('RESTORE_FAILED', report['status'])

    def test_bad_remote_host_rejected_without_download(self):
        self.release['assets'][0]['browser_download_url'] = 'https://evil.example/PARTS.json'
        self.save_release(self.release)
        report, counts = self.run_download(expected=1)
        self.assertEqual({}, counts)
        self.assertEqual('release_metadata', report['stage'])

    def test_wrong_release_rejected_without_download(self):
        self.release['tag_name'] = 'v2026.10.24'
        self.save_release(self.release)
        report, counts = self.run_download(expected=1)
        self.assertEqual({}, counts)
        self.assertEqual('release_metadata', report['stage'])

    def test_missing_part_or_digest_rejected(self):
        self.release['assets'][9]['digest'] = None
        self.save_release(self.release)
        _, counts = self.run_download(expected=1)
        self.assertEqual({}, counts)


if __name__ == '__main__':
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DownloadTests))
    (ROOT / 'evidence/download-fixtures.json').write_text(json.dumps(dict(tests=outcome.testsRun,
        failures=len(outcome.failures), errors=len(outcome.errors), records=RECORDS), ensure_ascii=False, indent=2), encoding='utf-8')
    raise SystemExit(not outcome.wasSuccessful())

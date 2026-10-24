"""Confirmed report entry-boundary regressions; local files only, no providers."""
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mixstudio.product import normalize_jd_link
from mixstudio.output_names import reserve_output
from mixstudio.settings import Settings, DEFAULTS
from mixstudio.service import Studio


class ConfirmedProductLinkTests(unittest.TestCase):
    def test_chinese_share_copy_does_not_become_url_path(self):
        for text, source in [
            ('https://u.jd.com/AbCdEf12点击链接', 'https://u.jd.com/AbCdEf12'),
            ('https://u.jd.com/AbCdEf12，复制此链接', 'https://u.jd.com/AbCdEf12'),
            ('https://item.jd.com/100012043978.html，快来看看', 'https://item.jd.com/100012043978.html'),
            ('商品【https://item.m.jd.com/product/100012043978.html】打开看看', 'https://item.jd.com/100012043978.html'),
            ("链接'https://u.jd.com/AbCdEf12'复制打开", 'https://u.jd.com/AbCdEf12'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(normalize_jd_link(text)['source'], source)

    def test_share_delimiter_does_not_relax_host_path_or_single_link_validation(self):
        for text in [
            'https://evil.example/100012043978.html，打开',
            'https://item.jd.com.evil.example/100012043978.html',
            'https://user@item.jd.com/100012043978.html',
            'https://item.jd.com:444/100012043978.html',
            'http://item.jd.com/100012043978.html',
            'https://item.jd.com/100012043978.html/evil打开',
            'https://u.jd.com/abc%2Fbad打开',
            'https://u.jd.com/AbCdEf12和https://u.jd.com/XyZ12',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_jd_link(text)


class ConfirmedOutputPathTests(unittest.TestCase):
    def test_blank_output_is_rejected_at_settings_boundary(self):
        settings = object.__new__(Settings)
        for value in ('', ' ', '\t'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings._validate('output_dir', value)
        # Optional paths stay optional; do not invent mandatory ASR/BGM config.
        for key in ('bgm_path', 'asr_model_path', 'download_dir'):
            settings._validate(key, '')

    def test_legacy_blank_output_is_recovered_without_overwriting_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = json.dumps({'output_dir':'', 'target_seconds':31}).encode()
            (root/'settings.json').write_bytes(raw)
            settings = Settings(root)
            self.assertEqual(settings.public()['output_dir'], DEFAULTS['output_dir'])
            self.assertEqual(settings.public()['target_seconds'], 31)
            self.assertEqual((root/'settings.json').read_bytes(), raw)
            self.assertEqual(len(list(root.glob('settings.invalid.*.json'))), 1)

    def test_output_reservation_never_falls_back_to_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = Path.cwd()
            try:
                os.chdir(temporary)
                for directory in ('', ' ', 'relative-output'):
                    with self.subTest(directory=directory), self.assertRaises(ValueError):
                        with reserve_output(directory, '测试商品'):
                            self.fail('Invalid directory reached the renderer')
            finally:
                os.chdir(previous)


class ConfirmedScanRaceTests(unittest.TestCase):
    def scan_with_disappearing_files(self, keep_good):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            gone = folder/'gone.mp4'; gone.write_bytes(b'synthetic')
            if keep_good:
                (folder/'good.mp4').write_bytes(b'synthetic')
            studio = object.__new__(Studio)
            studio.folder = lambda value: folder
            studio.store = SimpleNamespace(meta=Mock(), assets=lambda: [], reconcile_assets=Mock())
            studio.jobs = SimpleNamespace(submit=lambda kind, payload, work: work(threading.Event(), Mock()))
            studio._import_one = Mock(return_value={'sha256':'b'*64})
            studio._mark_library = Mock()
            real_stat = Path.stat
            calls = 0
            def racing_stat(path, *args, **kwargs):
                nonlocal calls
                if path == gone:
                    calls += 1
                    if calls == 2:
                        gone.unlink()
                        raise FileNotFoundError('Removed after is_file')
                return real_stat(path, *args, **kwargs)
            with patch.object(Path, 'stat', racing_stat):
                result = studio.scan(str(folder))
            self.assertEqual(result['imported'], int(keep_good))
            self.assertEqual(len(result['errors']), 1)
            self.assertEqual(result['errors'][0]['name'], 'gone.mp4')
            self.assertEqual(studio._import_one.call_count, int(keep_good))
            studio.store.reconcile_assets.assert_called_once_with([])

    def test_one_disappearing_file_does_not_abort_other_imports(self):
        self.scan_with_disappearing_files(True)

    def test_only_disappearing_file_reports_zero_imports_not_negative(self):
        self.scan_with_disappearing_files(False)


if __name__ == '__main__':
    unittest.main()

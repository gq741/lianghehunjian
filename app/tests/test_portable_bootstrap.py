"""Distribution-only boot tests. Every preference/credential is a local fixture."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def _load(name):
    path = Path(__file__).resolve().parents[1] / 'tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location('test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


boot = _load('portable_boot')
check = _load('portable_check')


class PortableBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='lianghe-bootstrap-test-')
        self.root = Path(self.temporary.name)
        self.model = self.root / 'models' / 'faster-whisper-small'
        self.model.mkdir(parents=True)
        for name in check.MODEL_FILES:
            (self.model / name).write_bytes(b'NON_INFERENCE_MODEL_FIXTURE')
        self.data = self.root / 'data'
        self.data.mkdir()
        self.secret = self.data / 'secrets.dpapi'
        self.secret.write_bytes(b'UNTOUCHED_CIPHERTEXT_FIXTURE')
        self.state = self.data / 'state.sqlite'
        self.state.write_bytes(b'UNTOUCHED_DATABASE_FIXTURE')

    def tearDown(self):
        try:
            self.assertEqual(self.secret.read_bytes(), b'UNTOUCHED_CIPHERTEXT_FIXTURE')
            self.assertEqual(self.state.read_bytes(), b'UNTOUCHED_DATABASE_FIXTURE')
        finally:
            self.temporary.cleanup()

    def test_fresh_user_gets_bundled_local_cpu_model(self):
        self.assertEqual(boot.prepare_settings(self.root, self.data), 'configured_bundled_model')
        values = json.loads((self.data / 'settings.json').read_text('utf-8'))
        self.assertEqual(values['asr_model_path'], str(self.model.resolve()))
        self.assertEqual(values['asr_device'], 'cpu')

    def test_relocation_migrates_missing_model_without_resetting_preferences(self):
        path = self.data / 'settings.json'
        path.write_text(json.dumps({'asr_model_path': str(self.root / 'old-location'),
                                    'bgm_gain': .37, 'target_seconds': 40}), encoding='utf-8')
        boot.prepare_settings(self.root, self.data)
        values = json.loads(path.read_text('utf-8'))
        self.assertEqual(values['asr_model_path'], str(self.model.resolve()))
        self.assertEqual(values['bgm_gain'], .37)
        self.assertEqual(values['target_seconds'], 40)

    def test_valid_existing_model_does_not_rewrite_configuration(self):
        path = self.data / 'settings.json'
        path.write_text(json.dumps({'asr_model_path': str(self.model.resolve()),
                                    'asr_device': 'cuda'}), encoding='utf-8')
        original = path.read_bytes()
        self.assertEqual(boot.prepare_settings(self.root, self.data), 'preserved_existing_model')
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_configuration_remains_original_bytes(self):
        path = self.data / 'settings.json'
        path.write_bytes(b'BROKEN_ORIGINAL_CONFIGURATION')
        self.assertEqual(boot.prepare_settings(self.root, self.data), 'preserved_preferences_needing_review')
        self.assertEqual(path.read_bytes(), b'BROKEN_ORIGINAL_CONFIGURATION')

    def test_missing_bundle_model_never_writes_configuration(self):
        (self.model / 'model.bin').unlink()
        with self.assertRaises(boot.StartupError):
            boot.prepare_settings(self.root, self.data)
        self.assertFalse((self.data / 'settings.json').exists())

    def test_model_presence_requires_all_offline_assets(self):
        self.assertTrue(check.model_ready(self.model))
        self.assertFalse(check.model_ready(self.root / 'missing'))
        (self.model / 'tokenizer.json').write_bytes(b'')
        self.assertFalse(check.model_ready(self.model))

    def test_worker_timeout_is_bounded_and_reports_failure(self):
        result, _ = check._run([sys.executable, '-I', '-B', '-c',
                               'import time;time.sleep(2)'], timeout=.05)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['ok'])

    def test_lock_checks_all_required_distribution_versions(self):
        names = ('Flask', 'pywebview', 'yt-dlp', 'faster-whisper', 'av', 'msgpack',
                 'rapidocr-onnxruntime', 'onnxruntime', 'pythonnet')
        (self.root / 'requirements-lock.txt').write_text(
            '\n'.join(name + '==1.0' for name in names), encoding='utf-8')
        with patch.object(check.importlib.metadata, 'version', return_value='1.0'):
            result = check.dependency_versions(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['checked'], len(names))

    def test_duplicate_distribution_lock_names_are_rejected(self):
        (self.root / 'requirements-lock.txt').write_text('Flask==1.0\nflask==1.0', encoding='utf-8')
        with patch.object(check.importlib.metadata, 'version', return_value='1.0'):
            result = check.dependency_versions(self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'lock_duplicate')

    def test_synthetic_hdr_has_explicit_ten_bit_pq_metadata_and_private_fonts(self):
        self.assertIn('format=yuv420p10le', check.SYNTHETIC_HDR_SOURCE)
        self.assertIn('color_primaries=bt2020', check.SYNTHETIC_HDR_SOURCE)
        self.assertIn('color_trc=smpte2084', check.SYNTHETIC_HDR_SOURCE)
        self.assertIn('colorspace=bt2020nc:range=limited', check.SYNTHETIC_HDR_SOURCE)
        self.assertTrue(check.SYNTHETIC_FILTER.startswith('zscale=t=linear:npl=100,'))
        self.assertIn('tonemap=tonemap=mobius', check.SYNTHETIC_FILTER)
        self.assertTrue(check.SYNTHETIC_FILTER.endswith('ass=check.ass:fontsdir=fonts'))

    def test_platform_requires_windows_ten_22h2_or_later(self):
        with patch.object(check.os, 'name', 'nt'), \
                patch.object(check.struct, 'calcsize', return_value=8), \
                patch.object(check.platform, 'machine', return_value='AMD64'):
            for build, accepted in ((19044, False), (19045, True), (22000, True), (26200, True)):
                with self.subTest(build=build), patch.object(check.sys, 'getwindowsversion',
                        return_value=SimpleNamespace(major=10, build=build), create=True):
                    self.assertEqual(check.supported_platform(), accepted)

    def test_platform_rejects_non_windows_arm_and_32_bit(self):
        with patch.object(check.os, 'name', 'posix'):
            self.assertFalse(check.supported_platform())
        with patch.object(check.os, 'name', 'nt'), \
                patch.object(check.struct, 'calcsize', return_value=4):
            self.assertFalse(check.supported_platform())
        with patch.object(check.os, 'name', 'nt'), \
                patch.object(check.struct, 'calcsize', return_value=8), \
                patch.object(check.platform, 'machine', return_value='ARM64'):
            self.assertFalse(check.supported_platform())


if __name__ == '__main__':
    unittest.main()

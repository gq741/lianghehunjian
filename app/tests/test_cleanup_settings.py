"""Caption background defaults and source-coordinate UI regression tests.

FIX_CLEANUP_SOURCE selects the original or repaired source tree for red/green
verification. All preferences are synthetic and live in temporary directories.
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

SOURCE = Path(os.environ.get('FIX_CLEANUP_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))

from mixstudio.settings import DEFAULTS, Settings


class CleanupSettingsTests(unittest.TestCase):
    def test_new_preferences_do_not_add_caption_background(self):
        with tempfile.TemporaryDirectory() as folder:
            preferences = Settings(folder)
            self.assertIs(DEFAULTS['caption_plate'], False)
            self.assertIs(preferences.values['caption_plate'], False)
            self.assertFalse(preferences.path.exists())

    def test_saved_preferences_without_plate_use_new_default_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            original = b'{"clean_mode":"delogo","clean_top_pct":88,"clean_height_pct":11}'
            path.write_bytes(original)
            preferences = Settings(folder)
            self.assertIs(preferences.values['caption_plate'], False)
            self.assertEqual(preferences.values['clean_top_pct'], 88)
            self.assertEqual(preferences.values['clean_height_pct'], 11)
            self.assertEqual(path.read_bytes(), original)

    def test_explicit_saved_plate_choices_remain_compatible(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'settings.json'
                original = json.dumps({'caption_plate': enabled}).encode('utf-8')
                path.write_bytes(original)
                self.assertIs(Settings(folder).values['caption_plate'], enabled)
                self.assertEqual(path.read_bytes(), original)

    def test_saved_plate_can_be_disabled_without_changing_cleanup_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            preferences = Settings(folder)
            preferences.update({'caption_plate': True, 'clean_mode': 'delogo',
                                'clean_top_pct': 88, 'clean_height_pct': 11})
            preferences.update({'caption_plate': False})
            loaded = Settings(folder)
            self.assertIs(loaded.values['caption_plate'], False)
            self.assertEqual(loaded.values['clean_mode'], 'delogo')
            self.assertEqual((loaded.values['clean_top_pct'], loaded.values['clean_height_pct']), (88, 11))
            self.assertFalse(loaded.secret_path.exists())

    def test_plate_setting_still_requires_boolean(self):
        with tempfile.TemporaryDirectory() as folder:
            preferences = Settings(folder)
            for invalid in ('false', 0, 1, None):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    preferences.update({'caption_plate': invalid})

    def test_frontend_default_matches_no_plate_backend_default(self):
        source = (SOURCE / 'web' / 'app.js').read_text('utf-8')
        match = re.search(r'caption_plate\s*:\s*(true|false)\b', source)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), 'false')

    def test_ui_explains_source_coordinates_and_optional_background(self):
        html = (SOURCE / 'web' / 'index.html').read_text('utf-8')
        self.assertIn('处理区域上沿 / 原视频高度 %', html)
        self.assertIn('处理区域高度 / 原视频高度 %', html)
        self.assertIn('添加字幕背景底板（可选）', html)
        self.assertIn('关闭后只显示描边文字，不添加黑色横框。', html)
        self.assertIn('先清理原字幕 / 水印，再在清理区域叠加新字幕。', html)

    def test_clip_preview_is_explicitly_source_not_cleaned_output(self):
        source = (SOURCE / 'web' / 'app.js').read_text('utf-8')
        self.assertIn('源片段预览', source)
        self.assertIn('此处为原片段，不是最终成片。', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)

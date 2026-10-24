import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine as engine, semantic_cache
from mixstudio.settings import Settings, DEFAULTS


def segments(count=17):
    return [{'segment_id': f's{i}', 'text': '同商品正面展示。', 'visual_hint': '同商品正面'} for i in range(count)]


def scenes(count=129):
    return [{'clip_id': f'c{i}', 'usable': True, 'confidence': .9, 'start': i * 3,
             'end': i * 3 + 3, 'text': '同商品正面', 'tags': ['外观'], 'asset_id': 'asset'} for i in range(count)]


def unmatched(system, body, **kwargs):
    return {'candidates': [], 'unmatched': [s['segment_id'] for s in body['segments']]}


class MatchingBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'cache'
        self.parts = segments()
        self.shots = scenes()
        self.context = {'draft': 'synthetic', 'revision': 1}

    def options(self):
        return dict(model='deepseek-flash', product_title='同商品', cache_dir=self.cache, cache_context=self.context)

    def old_cache(self, group, batch, result=None):
        normalized = engine._segments_for_match(self.parts)
        context = semantic_cache.fingerprint({'version': 1, 'endpoint': providers.DEEPSEEK_URL,
            'model': 'deepseek-flash', 'product_title': '同商品', 'prompt': engine.MATCH_SYSTEM,
            'segments': normalized, 'visual_scenes': self.shots, 'context': self.context})
        body = {'product_title': '同商品', 'segments': group, 'scenes': batch}
        identity = semantic_cache.fingerprint({'context': context, 'batch': body})
        self.cache.mkdir(exist_ok=True)
        path = self.cache / ('semantic-' + identity + '.json')
        path.write_text(json.dumps({'version': 1, 'identity': identity,
            'result': result or unmatched('', body)}), encoding='utf8')
        return path

    def test_17_segments_send_each_scene_batch_once_not_twice(self):
        with patch.object(engine, '_complete', side_effect=unmatched) as api:
            out = engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        self.assertEqual(api.call_count, 2)
        self.assertEqual(out['semantic_batch_count'], 2)
        self.assertEqual([len(call.args[1]['segments']) for call in api.call_args_list], [17, 17])
        self.assertEqual(sum(len(call.args[1]['scenes']) for call in api.call_args_list), 129)

    def test_64_segments_keeps_all_scenes_and_raises_output_budget(self):
        self.parts = segments(64)
        seen = []
        def complete(system, body, **kwargs):
            self.assertEqual(len(body['segments']), 64)
            self.assertGreater(kwargs['tokens'], 16000)
            self.assertLessEqual(kwargs['tokens'], 393216)
            seen.extend(c['clip_id'] for c in body['scenes'])
            return unmatched(system, body)
        with patch.object(engine, '_complete', side_effect=complete) as api:
            engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        self.assertEqual(api.call_count, 2)
        self.assertEqual(seen, [c['clip_id'] for c in self.shots])

    def test_budget_is_local_read_only_and_matches_actual_missing_requests(self):
        with patch.object(engine, '_complete') as paid:
            budget = engine.match_budget(self.parts, self.shots, **self.options())
        paid.assert_not_called()
        self.assertEqual(budget['scene_batch_count'], 2)
        self.assertEqual(budget['new_request_count'], 2)
        self.assertEqual(budget['available_scene_count'], 129)
        self.assertEqual(budget['cached_request_count'], 0)
        self.assertFalse(self.cache.exists())
        with patch.object(engine, '_complete', side_effect=unmatched):
            engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        with patch.object(engine, '_complete') as paid:
            budget = engine.match_budget(self.parts, self.shots, **self.options())
        paid.assert_not_called()
        self.assertEqual(budget['new_request_count'], 0)
        self.assertEqual(budget['cached_request_count'], 2)

    def test_all_old_16_group_caches_are_reused_without_reposting(self):
        normalized = engine._segments_for_match(self.parts)
        candidates, _ = engine._scenes_for_match(self.shots)
        for group in [normalized[:16], normalized[16:]]:
            for batch in [candidates[:128], candidates[128:]]:
                self.old_cache(group, batch)
        budget = engine.match_budget(self.parts, self.shots, **self.options())
        self.assertEqual(budget['new_request_count'], 0)
        self.assertEqual(budget['legacy_group_cache_hits'], 4)
        with patch.object(engine, '_complete') as paid:
            out = engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        paid.assert_not_called()
        self.assertEqual(out['unmatched'], [s['segment_id'] for s in self.parts])

    def test_partial_old_cache_only_submits_uncovered_segments(self):
        normalized = engine._segments_for_match(self.parts)
        candidates, _ = engine._scenes_for_match(self.shots)
        self.old_cache(normalized[:16], candidates[:128])
        budget = engine.match_budget(self.parts, self.shots, **self.options())
        self.assertEqual(budget['new_request_count'], 2)
        self.assertEqual(budget['legacy_group_cache_hits'], 1)
        with patch.object(engine, '_complete', side_effect=unmatched) as paid:
            engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        self.assertEqual([len(c.args[1]['segments']) for c in paid.call_args_list], [1, 17])
        with patch.object(engine, '_complete') as paid:
            engine.match_segments(self.parts, self.shots, api_key='synthetic-key', **self.options())
        paid.assert_not_called()

    def test_corrupt_legacy_cache_does_not_silently_repost(self):
        normalized = engine._segments_for_match(self.parts)
        candidates, _ = engine._scenes_for_match(self.shots)
        path = self.old_cache(normalized[:16], candidates[:128])
        path.write_text('{broken', encoding='utf8')
        with patch.object(engine, '_complete') as paid:
            with self.assertRaises(providers.ProviderError) as error:
                engine.match_budget(self.parts, self.shots, **self.options())
        self.assertEqual(error.exception.code, 'invalid_cache')
        paid.assert_not_called()

    def test_no_usable_scenes_budget_is_zero(self):
        budget = engine.match_budget(self.parts, [dict(s, usable=False) for s in self.shots], **self.options())
        self.assertEqual(budget['new_request_count'], 0)
        self.assertEqual(budget['scene_batch_count'], 0)


class SettingsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'settings.json'

    def assert_backup(self, raw):
        backups = list(self.root.glob('settings.invalid.*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_bad_json_does_not_block_start_and_preserves_original(self):
        raw = b'{broken-settings-synthetic'
        self.path.write_bytes(raw)
        config = Settings(self.root)
        self.assertEqual(config.values, DEFAULTS)
        self.assert_backup(raw)
        recovery = config.public()['settings_recovery']
        self.assertTrue(recovery['needed'])
        self.assertTrue(recovery['backup_saved'])
        self.assertNotIn('broken-settings-synthetic', json.dumps(recovery))
        self.assertNotIn(str(self.root), json.dumps(recovery))
        Settings(self.root)
        self.assert_backup(raw)

    def test_invalid_field_does_not_discard_valid_fields_or_credentials(self):
        raw = json.dumps({'bgm_gain': '', 'output_count': 99, 'target_seconds': 42,
                          'voice_id': 'valid-voice', 'output_dir': str(self.root / '输出')}).encode('utf8')
        self.path.write_bytes(raw)
        secret = self.root / 'secrets.dpapi'
        secret.write_bytes(b'original-ciphertext-synthetic')
        with patch.object(Settings, '_secrets', return_value={'glm': 'not-a-real-secret'}):
            config = Settings(self.root)
            self.assertTrue(config.public()['glm_key_set'])
        self.assertEqual(config.values['target_seconds'], 42)
        self.assertEqual(config.values['voice_id'], 'valid-voice')
        self.assertEqual(config.values['output_dir'], str(self.root / '输出'))
        self.assertEqual(config.values['bgm_gain'], DEFAULTS['bgm_gain'])
        self.assertEqual(config.values['output_count'], DEFAULTS['output_count'])
        self.assertEqual(config.public()['settings_recovery']['invalid_fields'], ['bgm_gain', 'output_count'])
        self.assertEqual(secret.read_bytes(), b'original-ciphertext-synthetic')
        self.assert_backup(raw)

    def test_non_object_and_non_utf8_are_recovered(self):
        for raw in [b'[]', b'null', b'42', b'\xff\xfeinvalid']:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'settings.json').write_bytes(raw)
                config = Settings(root)
                self.assertTrue(config.public()['settings_recovery']['needed'])
                self.assertEqual(config.values, DEFAULTS)
                self.assertEqual(len(list(root.glob('settings.invalid.*.json'))), 1)

    def test_backup_failure_keeps_start_working_and_blocks_overwrite(self):
        raw = b'{broken'
        self.path.write_bytes(raw)
        original_open = Path.open
        def fail_backup(path, *args, **kwargs):
            if path.name.startswith('settings.invalid.'):
                raise OSError('synthetic path detail must not leak')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_backup):
            config = Settings(self.root)
            notice = config.public()['settings_recovery']
            self.assertTrue(notice['needed'])
            self.assertFalse(notice['backup_saved'])
            with self.assertRaises(ValueError):
                config.update({'bgm_gain': .2})
        self.assertEqual(self.path.read_bytes(), raw)
        config.update({'bgm_gain': .2})
        self.assertEqual(config.values['bgm_gain'], .2)
        self.assertFalse(config.public()['settings_recovery']['needed'])
        self.assertEqual(next(self.root.glob('settings.invalid.*.json')).read_bytes(), raw)

    def test_failed_backup_flush_is_retried_before_overwriting_original(self):
        raw = b'{broken-fsync-fixture'
        self.path.write_bytes(raw)
        with patch('mixstudio.settings.os.fsync', side_effect=OSError('injected flush failure')):
            config = Settings(self.root)
            self.assertFalse(config.public()['settings_recovery']['backup_saved'])
            with self.assertRaises(ValueError):
                config.update({'bgm_gain': .2})
        self.assertEqual(self.path.read_bytes(), raw)
        config.update({'bgm_gain': .2})
        self.assertEqual(next(self.root.glob('settings.invalid.*.json')).read_bytes(), raw)
        self.assertEqual(Settings(self.root).values['bgm_gain'], .2)

    def test_explicit_save_clears_recovery_only_after_success(self):
        self.path.write_text('{broken', encoding='utf8')
        config = Settings(self.root)
        with patch('mixstudio.settings._atomic', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                config.update({'bgm_gain': .25})
        self.assertTrue(config.public()['settings_recovery']['needed'])
        config.update({'bgm_gain': .25})
        self.assertFalse(config.public()['settings_recovery']['needed'])
        self.assertFalse(Settings(self.root).public()['settings_recovery']['needed'])

    def test_numeric_empty_strings_are_never_coerced_to_zero(self):
        config = Settings(self.root)
        for key in ['bgm_gain', 'target_seconds', 'output_count', 'transition_seconds', 'fps']:
            for empty in ['', ' ', '\t']:
                with self.subTest(key=key, empty=empty), self.assertRaises(ValueError):
                    config.update({key: empty})
        self.assertEqual(config.values, DEFAULTS)
        self.assertFalse(self.path.exists())

    def test_healthy_settings_are_read_only_and_have_no_recovery(self):
        raw = b'{"bgm_gain":0.3,"target_seconds":27}'
        self.path.write_bytes(raw)
        config = Settings(self.root)
        self.assertEqual(config.values['bgm_gain'], .3)
        self.assertFalse(config.public()['settings_recovery']['needed'])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(list(self.root.glob('settings.invalid.*.json')))


if __name__ == '__main__':
    unittest.main(verbosity=2)

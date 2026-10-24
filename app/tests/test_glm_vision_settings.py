"""GLM adapter/settings migration tests; credentials and HTTP are synthetic."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mixstudio import providers, settings, vision
from test_vision import observation, envelope, respond

GLM_ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
QWEN_ENDPOINT = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'


class GlmVisionTests(unittest.TestCase):
    def test_default_sampling_and_cache_version(self):
        self.assertEqual(vision.DEFAULT_ENDPOINT, GLM_ENDPOINT)
        self.assertEqual(vision.DEFAULT_MODEL, 'glm-5.3-flash')
        self.assertEqual(vision.ALGORITHM_VERSION, 2)
        self.assertEqual(vision.WINDOW_SECONDS, 3)
        self.assertEqual(len(vision.FRAME_FRACTIONS), 3)
        self.assertEqual(vision.SCENES_PER_REQUEST, 2)
        self.assertEqual(vision.MAX_REQUESTS, 45)
        plan, _ = vision._plan('a' * 64, 1000)
        self.assertEqual(len(plan), 90)
        self.assertTrue(all(len(s['sample_times']) == 3 for s in plan))

    def test_glm_payload_thinking_and_frame_counts_are_provider_specific(self):
        batch = [{'clip_id': 'one'}, {'clip_id': 'tail'}]
        images = {'one': [b'jpeg'] * 3, 'tail': [b'jpeg'] * 2}
        with patch.object(providers, '_post', side_effect=respond) as post:
            result = vision._request(batch, images, 'fixture', GLM_ENDPOINT, 'glm-5.3-flash', 'fake', None)
        self.assertEqual([value['clip_id'] for value in result], ['one', 'tail'])
        payload = post.call_args.args[1]
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertNotIn('enable_thinking', payload)
        clips = json.loads(payload['messages'][1]['content'][0]['text'])['clips']
        self.assertEqual([s['frames'] for s in clips], [3, 2])
        self.assertEqual(sum(x['type'] == 'image_url' for x in payload['messages'][1]['content']), 5)

    def test_provider_endpoint_and_model_may_not_be_crossed(self):
        for endpoint, model in [(GLM_ENDPOINT, 'qwen-vl-plus'), (QWEN_ENDPOINT, 'glm-5.3-flash'), (GLM_ENDPOINT, 'glm-4-flash')]:
            with self.subTest(endpoint=endpoint, model=model), self.assertRaises(providers.ProviderError):
                vision.validate_config(endpoint, model)
        self.assertEqual(vision.validate_config(GLM_ENDPOINT, 'glm-5.3-flash'), (GLM_ENDPOINT, 'glm-5.3-flash'))
        self.assertEqual(vision.validate_config(QWEN_ENDPOINT, 'qwen-vl-plus'), (QWEN_ENDPOINT, 'qwen-vl-plus'))


class GlmSettingsTests(unittest.TestCase):
    def test_old_settings_migrate_only_in_memory_and_old_key_never_routes_to_glm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = json.dumps({'vision_endpoint': QWEN_ENDPOINT, 'vision_model': 'qwen-vl-plus'})
            (root / 'settings.json').write_text(original, encoding='utf-8')
            with patch.object(settings.Settings, '_secrets', return_value={'vision': 'old-qwen-fixture'}):
                config = settings.Settings(root)
                public = config.public()
                self.assertEqual(public['vision_provider'], 'glm')
                self.assertEqual(public['vision_endpoint'], GLM_ENDPOINT)
                self.assertEqual(public['vision_model'], 'glm-5.3-flash')
                self.assertFalse(public['glm_key_set'])
                self.assertFalse(public['vision_key_set'])
                self.assertEqual(config.secret('vision'), '')
                self.assertEqual(config.secret('glm'), '')
            self.assertEqual((root / 'settings.json').read_text('utf-8'), original)
            self.assertFalse((root / 'secrets.dpapi').exists())

    def test_explicit_glm_key_preserves_old_key_and_routes_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = settings.Settings(root)
            old = {'vision': 'old-qwen-fixture', 'deepseek': 'old-deepseek-fixture'}
            encrypted = []
            def encrypt(raw, decrypt=False):
                if decrypt:
                    return raw
                encrypted.append(json.loads(raw))
                return raw
            with patch.object(config, '_secrets', return_value=old.copy()), patch.object(settings, '_dpapi', side_effect=encrypt):
                config.update({'glm_api_key': 'new-glm-fixture'})
            self.assertEqual(encrypted[-1]['vision'], old['vision'])
            self.assertEqual(encrypted[-1]['glm'], 'new-glm-fixture')
            with patch.object(config, '_secrets', return_value=encrypted[-1]):
                self.assertEqual(config.secret('vision'), 'new-glm-fixture')
                self.assertEqual(config.secret('glm'), 'new-glm-fixture')
                self.assertTrue(config.public()['glm_key_set'])
                self.assertTrue(config.public()['vision_key_set'])
            self.assertNotIn('new-glm-fixture', config.path.read_text('utf-8'))

    def test_old_key_field_does_not_accidentally_save_to_glm(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings.Settings(tmp)
            with self.assertRaises(ValueError):
                config.update({'vision_api_key': 'old-ui-qwen-fixture'})
            self.assertFalse(config.secret_path.exists())

    def test_existing_encrypted_qwen_key_and_settings_bytes_are_read_only_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_bytes = json.dumps({'vision_model': 'qwen3-vl-plus', 'vision_endpoint': QWEN_ENDPOINT}, indent=2).encode()
            secret_bytes = base64.b64encode(json.dumps({'vision': 'qwen-fixture-only'}).encode())
            (root / 'settings.json').write_bytes(config_bytes)
            (root / 'secrets.dpapi').write_bytes(secret_bytes)
            with patch.object(settings, '_dpapi', side_effect=lambda raw, decrypt=False: raw), patch.object(settings, '_atomic') as write:
                config = settings.Settings(root)
                self.assertFalse(config.public()['glm_key_set'])
                self.assertEqual(config.secret('vision'), '')
                write.assert_not_called()
            self.assertEqual((root / 'settings.json').read_bytes(), config_bytes)
            self.assertEqual((root / 'secrets.dpapi').read_bytes(), secret_bytes)

    def test_saved_glm_preferences_remain_and_corrupt_secrets_do_not_appear_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = {'vision_provider': 'glm', 'vision_model': 'glm-5.3-flash', 'vision_endpoint': GLM_ENDPOINT}
            (root / 'settings.json').write_text(json.dumps(saved), encoding='utf-8')
            config = settings.Settings(root)
            with patch.object(config, '_secrets', side_effect=ValueError('fixture corrupt')):
                public = config.public()
                self.assertTrue(all(public[key] == value for key, value in saved.items()))
                self.assertFalse(public['glm_key_set'])
                self.assertFalse(public['vision_key_set'])
            for values in ({'vision_provider': 'qwen'}, {'vision_model': 'qwen-vl-plus'},
                           {'vision_endpoint': QWEN_ENDPOINT}, {'vision_model': None}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    config.update(values)


if __name__ == '__main__':
    unittest.main()

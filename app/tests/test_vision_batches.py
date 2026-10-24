"""New GLM sampling contract through the production pipeline; no real API calls."""
import hashlib
import json
import unittest
from unittest.mock import patch

import test_vision as fixtures
from mixstudio import providers, vision


class GlmBatchContractTests(unittest.TestCase):
    setUp = fixtures.VisionPipelineTests.setUp
    analyze = fixtures.VisionPipelineTests.analyze

    def test_three_frame_pipeline_declares_actual_tail_count_and_reuses_cache(self):
        self.probe.return_value['duration'] = 3.1
        result = self.analyze()
        self.assertEqual(self.frame.call_count, 6)  # Three requested positions per window.
        self.assertEqual(self.post.call_count, 1)
        request = self.post.call_args.args[1]
        content = request['messages'][1]['content']
        counts = [c['frames'] for c in json.loads(content[0]['text'])['clips']]
        self.assertEqual(counts, [3, 1])  # Identical short-tail JPEGs are not repeated.
        self.assertEqual([s['sampled_frames'] for s in result['scenes']], [3, 1])
        self.assertEqual([len(s['quality']) for s in result['scenes']], [3, 1])
        self.assertIn('不是逐帧检查', result['warnings'][0])
        self.assertEqual(self.analyze(), result)
        self.assertEqual(self.frame.call_count, 6)
        self.assertEqual(self.post.call_count, 1)

    def test_maximum_90_windows_270_samples_45_explicit_glm_batches(self):
        self.probe.return_value['duration'] = 999
        before = self.source.read_bytes()
        result = self.analyze()
        self.assertEqual(len(result['scenes']), 90)
        self.assertEqual(self.frame.call_count, 270)
        self.assertEqual(self.post.call_count, 45)
        for request in self.post.call_args_list:
            endpoint, payload, _ = request.args
            self.assertEqual(endpoint, 'https://open.bigmodel.cn/api/paas/v4/chat/completions')
            self.assertEqual(payload['model'], 'glm-5.3-flash')
            self.assertEqual(payload['thinking'], {'type': 'enabled'})
            content = payload['messages'][1]['content']
            self.assertEqual(sum(c['type'] == 'image_url' for c in content), 6)
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), vision.MAX_REQUEST_BYTES)
        self.assertEqual(self.source.read_bytes(), before)

    def test_v1_cache_is_kept_but_never_reused_by_new_sampling(self):
        old_identity = {'version': 1, 'sha256': self.asset['sha256'], 'duration': 6,
                        'model': 'qwen-vl-plus',
                        'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                        'product_title': self.options['product_title']}
        old_path = vision._cache_path(self.cache, old_identity)
        original = json.dumps({'identity': old_identity, 'records': []}).encode()
        old_path.write_bytes(original)
        result = self.analyze()
        self.assertEqual(len(result['scenes']), 2)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.frame.call_count, 6)
        self.assertEqual(old_path.read_bytes(), original)
        files = list(self.cache.glob('vision-*.json'))
        self.assertEqual(len(files), 2)
        new = json.loads(next(p for p in files if p != old_path).read_text('utf-8'))
        self.assertEqual(new['identity']['version'], 2)
        self.assertEqual(new['identity']['sampling']['frame_fractions'], [.1, .5, .9])
        self.assertEqual(new['identity']['sampling']['max_scenes'], 90)
        self.assertIn('prompt_sha256', new['identity'])

    def test_prompt_and_sampling_parameters_partition_cache(self):
        self.analyze()
        with patch.object(vision, 'SYSTEM_PROMPT', vision.SYSTEM_PROMPT + '\nfixture prompt revision'):
            self.analyze()
        with patch.object(vision, 'FRAME_FRACTIONS', (.11, .51, .91)):
            self.analyze()
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(len(list(self.cache.glob('vision-*.json'))), 3)

    def test_request_image_and_byte_limits_before_provider(self):
        batch = [{'clip_id': 'one'}, {'clip_id': 'two'}]
        for images in ({'one': [b'jpeg'] * 6, 'two': [b'jpeg'] * 5},
                       {'one': [b'x' * (vision.MAX_JPEG_BYTES + 1)], 'two': [b'jpeg']}):
            with self.subTest(images=[len(v) for v in images.values()]), self.assertRaises(providers.ProviderError) as error:
                vision._request(batch, images, 'fixture', vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, 'fake', None)
            self.assertEqual(error.exception.code, 'request_too_large')
        with patch.object(vision, 'MAX_REQUEST_BYTES', 64), self.assertRaises(providers.ProviderError):
            vision._request(batch, {'one': [b'jpeg'], 'two': [b'jpeg']}, 'fixture',
                            vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, 'fake', None)
        self.post.assert_not_called()

    def test_full_window_missing_quality_is_not_mistaken_for_tail_dedup(self):
        self.analyze()
        cache = next(self.cache.glob('vision-*.json'))
        data = json.loads(cache.read_text('utf-8'))
        data['records'][0]['quality'] = data['records'][0]['quality'][:1]
        cache.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(providers.ProviderError) as error:
            self.analyze()
        self.assertEqual(error.exception.code, 'invalid_cache')
        self.assertEqual(self.post.call_count, 1)


if __name__ == '__main__':
    unittest.main()

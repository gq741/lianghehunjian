"""Offline recovery, 3-frame sampling and verified legacy-cache migration."""
import hashlib
import json
import threading
import unittest
from unittest.mock import patch

from mixstudio import providers, vision
import test_vision as fixtures
from test_vision import observation, respond

LEGACY = (.1, .3, .5, .7, .9)
PROMPT_SHA = '40ae8dd0753161be6a0a63ecf4dd4817813bcc0b8f342913cd041ce2b7645b31'


class VisionRecoveryTests(unittest.TestCase):
    setUp = fixtures.VisionPipelineTests.setUp
    analyze = fixtures.VisionPipelineTests.analyze

    def legacy_cache(self, duration=18, count=2, **changes):
        with patch.object(vision, 'FRAME_FRACTIONS', LEGACY):
            planned, _ = vision._plan(self.asset['sha256'], duration)
            identity = vision._cache_identity(self.asset['sha256'], duration,
                self.options['endpoint'], self.options['model'], self.options['product_title'])
        identity.update(changes)
        records = {s['clip_id']: {'planned': s, 'observation': observation(s['clip_id']),
            'quality': [{'mean': 100 + i, 'black_ratio': 0} for i in range(5)]} for s in planned[:count]}
        path = vision._cache_path(self.cache, identity)
        vision._save_cache(path, identity, records)
        return path, path.read_bytes(), records

    def test_default_three_frames_six_per_request_and_prompt_unchanged(self):
        self.assertEqual(vision.FRAME_FRACTIONS, (.1, .5, .9))
        self.assertEqual(vision.MAX_BATCH_IMAGES, 6)
        self.assertEqual(vision.ALGORITHM_VERSION, 2)
        self.assertEqual(hashlib.sha256(vision.SYSTEM_PROMPT.encode()).hexdigest(), PROMPT_SHA)
        result = self.analyze()
        self.assertEqual(self.frame.call_count, 6)
        self.assertEqual([s['sampled_frames'] for s in result['scenes']], [3, 3])

    def test_partial_failure_preserves_success_and_processes_distinct_next_batch(self):
        self.probe.return_value['duration'] = 18
        calls = []
        def response(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise providers.ProviderError('upstream-secret', code='timeout', result_unknown=True)
            return respond(*args, **kwargs)
        self.post.side_effect = response
        result = self.analyze(allow_partial=True)
        self.assertEqual([(s['start'], s['end']) for s in result['scenes']], [(0,3),(3,6),(12,15),(15,18)])
        summary = result['analysis_summary']
        self.assertEqual((summary['planned'], summary['completed'], summary['pending']), (6,4,2))
        self.assertFalse(summary['complete'])
        self.assertFalse(summary['pause_new_requests'])
        self.assertEqual(len(summary['failed_batches']), 1)
        self.assertEqual(summary['failed_batches'][0]['batch_index'], 2)
        self.assertNotIn('upstream-secret', json.dumps(result))
        self.post.side_effect = respond
        resumed = self.analyze(allow_partial=True)
        self.assertTrue(resumed['analysis_summary']['complete'])
        self.assertEqual(self.post.call_count, 4)  # never automatically repeat failed batch

    def test_two_consecutive_failures_stop_current_asset(self):
        self.probe.return_value['duration'] = 30
        self.post.side_effect = providers.ProviderError('private', code='network', result_unknown=True)
        result = self.analyze(allow_partial=True)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(result['scenes'], [])
        self.assertEqual(result['analysis_summary']['stop_reason'], 'consecutive_failures')
        self.assertTrue(result['analysis_summary']['pause_new_requests'])

    def test_success_resets_consecutive_failure_count(self):
        self.probe.return_value['duration'] = 30
        def response(*args, **kwargs):
            if self.post.call_count in (1,3,5):
                raise providers.ProviderError('private', code='timeout', result_unknown=True)
            return respond(*args, **kwargs)
        self.post.side_effect = response
        result = self.analyze(allow_partial=True)
        self.assertEqual(self.post.call_count, 5)
        self.assertEqual(result['analysis_summary']['completed'], 4)
        self.assertEqual(len(result['analysis_summary']['failed_batches']), 3)
        self.assertFalse(result['analysis_summary']['pause_new_requests'])

    def test_server_failure_or_rate_limit_pause_new_requests_immediately(self):
        for code in ('http_429', 'http_500', 'http_502', 'http_503', 'http_504'):
            with self.subTest(code=code):
                self.post.reset_mock()
                self.post.side_effect = providers.ProviderError('private', code=code, result_unknown=True)
                result = self.analyze(allow_partial=True)
                self.assertEqual(self.post.call_count, 1)
                self.assertTrue(result['analysis_summary']['pause_new_requests'])
                self.assertEqual(result['analysis_summary']['stop_reason'], 'provider_unavailable')

    def test_auth_configuration_balance_and_cancellation_are_fatal(self):
        for code in ('http_400','http_401','http_402','http_403','http_404','http_422',
                     'invalid_input','invalid_vision_input','invalid_cache','cache_write','source_changed','cancelled'):
            with self.subTest(code=code):
                self.post.reset_mock()
                self.post.side_effect = providers.ProviderError('private', code=code)
                with self.assertRaises(providers.ProviderError) as caught:
                    self.analyze(allow_partial=True)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.post.call_count, 1)

    def test_http_429_balance_business_code_is_fatal_not_rate_limit(self):
        failure = providers.ProviderError('private', code='http_429')
        failure.diagnostic = {'http_status':429, 'business_code':'1113'}
        self.post.side_effect = failure
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze(allow_partial=True)
        self.assertEqual(caught.exception.code, 'http_429')
        self.assertEqual(caught.exception.diagnostic['business_code'], '1113')
        self.assertIn('余额', str(caught.exception))
        self.assertNotIn('限流', str(caught.exception))
        self.assertEqual(self.post.call_count, 1)

    def test_default_mode_still_raises(self):
        self.post.side_effect = providers.ProviderError('private', code='timeout', result_unknown=True)
        with self.assertRaises(providers.ProviderError):
            self.analyze()
        self.assertEqual(self.post.call_count, 1)

    def test_cache_only_empty_needs_no_credentials_frames_or_calls(self):
        result = self.analyze(cache_only=True, api_key='')
        self.assertEqual(result['scenes'], [])
        self.assertEqual(result['analysis_summary']['pending'], 2)
        self.assertEqual(result['analysis_summary']['stop_reason'], 'cache_only')
        self.post.assert_not_called()
        self.frame.assert_not_called()

    def test_cache_only_complete_reuses_all_results(self):
        first = self.analyze()
        self.post.reset_mock(); self.frame.reset_mock()
        result = self.analyze(cache_only=True, api_key='')
        self.assertEqual(result['scenes'], first['scenes'])
        self.assertTrue(result['analysis_summary']['complete'])
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_five_frame_cache_migrates_with_original_samples_and_quality(self):
        self.probe.return_value['duration'] = 18
        old, before, records = self.legacy_cache()
        result = self.analyze(cache_only=True, api_key='')
        self.assertEqual(len(result['scenes']), 2)
        self.assertEqual([s['sampled_frames'] for s in result['scenes']], [5,5])
        for scene in result['scenes']:
            original = records[scene['clip_id']]
            self.assertEqual(scene['sample_times'], original['planned']['sample_times'])
            self.assertEqual(scene['quality'], original['quality'])
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(len(list(self.cache.glob('vision-*.json'))), 2)
        self.post.assert_not_called(); self.frame.assert_not_called()
        self.assertEqual(self.analyze(cache_only=True)['scenes'], result['scenes'])

    def test_migrated_five_and_new_three_frame_records_coexist(self):
        self.probe.return_value['duration'] = 18
        old, before, _ = self.legacy_cache()
        result = self.analyze(allow_partial=True)
        self.assertEqual([s['sampled_frames'] for s in result['scenes']], [5,5,3,3,3,3])
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.frame.call_count, 12)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.analyze(cache_only=True)['scenes'], result['scenes'])

    def test_legacy_cache_wrong_title_not_reused(self):
        self.probe.return_value['duration'] = 18
        old, before, _ = self.legacy_cache(product_title='different product')
        result = self.analyze(cache_only=True)
        self.assertEqual(result['scenes'], [])
        self.assertEqual(old.read_bytes(), before)

    def test_legacy_other_source_model_endpoint_and_prompt_are_not_reused(self):
        self.probe.return_value['duration'] = 18
        for change in ({'sha256': 'b'*64}, {'model': 'other-model'},
                       {'endpoint': 'https://example.invalid'}, {'prompt_sha256': 'c'*64}):
            with self.subTest(change=change):
                old, before, _ = self.legacy_cache(**change)
                self.assertEqual(self.analyze(cache_only=True)['scenes'], [])
                self.assertEqual(old.read_bytes(), before)
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_seventy_one_legacy_records_retained_without_paid_regeneration(self):
        self.probe.return_value['duration'] = 270
        old, before, original = self.legacy_cache(duration=270, count=71)
        result = self.analyze(cache_only=True)
        self.assertEqual(result['analysis_summary']['completed'], 71)
        self.assertEqual(result['analysis_summary']['pending'], 19)
        self.assertEqual({s['clip_id'] for s in result['scenes']}, set(original))
        self.assertTrue(all(len(s['quality']) == 5 for s in result['scenes']))
        self.assertEqual(old.read_bytes(), before)
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_legacy_duration_migration_only_keeps_identical_boundaries(self):
        self.probe.return_value.update(duration=6.1, video_duration=6.,
            format_start_time=0., video_start_time=0., video_end_time=6.)
        old, before, _ = self.legacy_cache(duration=6.1, count=3)
        result = self.analyze(cache_only=True)
        self.assertEqual([(s['start'],s['end']) for s in result['scenes']], [(0,3),(3,6)])
        self.assertTrue(result['analysis_summary']['complete'])
        self.assertEqual(old.read_bytes(), before)
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_legacy_sample_time_tamper_is_fatal_before_new_requests(self):
        self.probe.return_value['duration'] = 18
        old, _, _ = self.legacy_cache()
        data = json.loads(old.read_text('utf-8'))
        data['records'][0]['planned']['sample_times'][0] = .5
        old.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze(allow_partial=True)
        self.assertEqual(caught.exception.code, 'invalid_cache')
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_cache_write_failure_is_fatal_even_in_partial_mode(self):
        failure = providers.ProviderError('local disk error', code='cache_write', result_unknown=True)
        with patch.object(vision, '_save_cache', side_effect=failure):
            with self.assertRaises(providers.ProviderError) as caught:
                self.analyze(allow_partial=True)
        self.assertEqual(caught.exception.code, 'cache_write')
        self.assertEqual(self.post.call_count, 1)

    def test_corrupt_legacy_cache_is_not_silently_regenerated(self):
        self.probe.return_value['duration'] = 18
        old, _, _ = self.legacy_cache()
        data = json.loads(old.read_text('utf-8'))
        data['records'][0]['quality'] = data['records'][0]['quality'][:3]
        old.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze(allow_partial=True)
        self.assertEqual(caught.exception.code, 'invalid_cache')
        self.post.assert_not_called(); self.frame.assert_not_called()

    def test_cancelled_cache_only_does_not_return_success(self):
        event = threading.Event(); event.set()
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze(cache_only=True, cancel=event)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_provider_failure_diagnostic_survives_both_vision_wrappers(self):
        error = providers.ProviderError('private', code='http_503', result_unknown=True)
        error.diagnostic = {'http_status':503, 'business_code':'500', 'request_id':'fixture-123',
                            'raw':'never keep this'}
        wrapped = vision.stage_error(vision._batch_error(error, 1, 3, 2), 'fixture')
        self.assertEqual(wrapped.diagnostic['http_status'], 503)
        self.assertEqual(wrapped.diagnostic['business_code'], '500')
        self.assertNotIn('raw', wrapped.diagnostic)
        self.assertIn('503', str(wrapped))
        self.assertIn('500', str(wrapped))

    def test_format_error_diagnostic_hash_not_raw_response(self):
        raw = b'{"choices":[{"finish_reason":"stop","message":{"content":"private-not-json"}}]}'
        self.post.side_effect = None
        self.post.return_value = (raw, 'application/json')
        result = self.analyze(allow_partial=True)
        diagnostic = result['analysis_summary']['failed_batches'][0]['diagnostic']
        self.assertEqual(diagnostic['response_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(diagnostic['response_bytes'], len(raw))
        self.assertIn('response_shape', diagnostic)
        self.assertNotIn('private-not-json', json.dumps(result))


if __name__ == '__main__':
    unittest.main()


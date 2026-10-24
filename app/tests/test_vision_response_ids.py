"""Offline regression: short request-local IDs, strict mapping and resumable cache."""
import hashlib
import json
import unittest
from unittest.mock import patch

from mixstudio import providers, vision
import test_vision as fixtures


class VisionResponseIdTests(unittest.TestCase):
    def setUp(self):
        self.ids = ['c668a3eabdd663739664d367', 'bd535ba269fb9bc9bb6c78b0']
        self.batch = [{'clip_id': cid} for cid in self.ids]
        self.images = {cid: [b'jpeg'] * 3 for cid in self.ids}

    def request(self):
        return vision._request(self.batch, self.images, 'fixture',
            vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, 'test-key-not-real', None)

    def test_short_aliases_replace_all_wire_ids_and_reordered_results_map_exactly(self):
        def answer(url, payload, key, **kwargs):
            wire = json.loads(payload['messages'][1]['content'][0]['text'])['clips']
            self.assertEqual([item['clip_id'] for item in wire], ['c1', 'c2'])
            encoded = json.dumps(payload)
            self.assertTrue(all(cid not in encoded for cid in self.ids))
            self.assertEqual(sum(item['type'] == 'image_url'
                for item in payload['messages'][1]['content']), 6)
            return fixtures.envelope([
                fixtures.observation('c2', description='second'),
                fixtures.observation('c1', description='first')])
        with patch.object(providers, '_post', side_effect=answer) as post:
            result = self.request()
        self.assertEqual([(v['clip_id'], v['description']) for v in result],
                         [(self.ids[1], 'second'), (self.ids[0], 'first')])
        self.assertEqual(post.call_count, 1)

    def test_response_issues_remain_distinct_after_both_stage_wrappers(self):
        cases = [
            ([fixtures.observation('c1')], 'missing_ids'),
            ([fixtures.observation('c1'), fixtures.observation('c1')], 'duplicate_ids'),
            ([fixtures.observation('c1'), fixtures.observation('c3')], 'unknown_ids'),
            ([fixtures.observation('c1'), fixtures.observation(self.ids[1])], 'unknown_ids'),
            ([fixtures.observation('c1'), fixtures.observation('C2')], 'unknown_ids'),
            ([fixtures.observation('c1'), fixtures.observation('c2 ')], 'unknown_ids'),
            ([fixtures.observation('c1'), fixtures.observation('c2', flags=['invented'])], 'schema_invalid'),
        ]
        for values, issue in cases:
            with self.subTest(issue=issue, ids=[v['clip_id'] for v in values]):
                with patch.object(providers, '_post', return_value=fixtures.envelope(values)) as post:
                    with self.assertRaises(providers.ProviderError) as caught:
                        self.request()
                error = vision.stage_error(vision._batch_error(caught.exception, 27, 45, 2),
                                           '画面识别 · 素材 1/3')
                self.assertEqual(error.code, 'invalid_vision_response')
                self.assertEqual(error.response_issue, issue)
                self.assertTrue(error.result_unknown)
                self.assertEqual(error.vision_batch, (27, 45, 2))
                self.assertEqual(post.call_count, 1)
                self.assertNotIn(self.ids[1], str(error))

    def test_json_truncation_and_filter_are_not_reported_as_id_mismatch(self):
        cases = [('stop', '{', 'json_invalid'), ('length', '{}', 'truncated'),
                 ('content_filter', '', 'blocked'), ('sensitive', '', 'blocked')]
        for finish, content, issue in cases:
            raw = json.dumps({'choices': [{'finish_reason': finish,
                'message': {'content': content}}]}).encode()
            with self.subTest(issue=issue), patch.object(providers, '_post',
                    return_value=(raw, 'application/json')) as post:
                with self.assertRaises(providers.ProviderError) as caught:
                    self.request()
                self.assertEqual(caught.exception.response_issue, issue)
                self.assertEqual(post.call_count, 1)

    def test_captured_long_id_typo_is_rejected_not_fuzzily_repaired(self):
        values = [fixtures.observation(self.ids[0]),
                  fixtures.observation('bd535ba269fb9bc9bc9bb6c78b0')]
        with patch.object(providers, '_post', return_value=fixtures.envelope(values)):
            with self.assertRaises(providers.ProviderError) as caught:
                self.request()
        self.assertEqual(caught.exception.response_issue, 'unknown_ids')

    def test_aliases_are_local_to_request_and_never_persist_as_canonical_ids(self):
        with patch.object(providers, '_post', side_effect=fixtures.respond) as post:
            first = self.request()
            self.ids = ['d' * 24, 'e' * 24]
            self.batch = [{'clip_id': cid} for cid in self.ids]
            self.images = {cid: [b'jpeg'] * 3 for cid in self.ids}
            second = self.request()
        self.assertEqual([v['clip_id'] for v in first],
                         ['c668a3eabdd663739664d367', 'bd535ba269fb9bc9bb6c78b0'])
        self.assertEqual([v['clip_id'] for v in second], self.ids)
        self.assertEqual(post.call_count, 2)

    def test_untrusted_diagnostic_never_reaches_display(self):
        error = providers.ProviderError('secret-private-provider-text',
            code='invalid_vision_response', result_unknown=True)
        error.response_issue = 'secret-private-provider-text'
        result = vision.stage_error(vision._batch_error(error, 27, 45, 2), '画面识别')
        self.assertNotIn('secret-private', str(result))
        self.assertFalse(hasattr(result, 'response_issue'))

    def test_original_prompt_and_cache_identity_remain_compatible(self):
        self.assertEqual(hashlib.sha256(vision.SYSTEM_PROMPT.encode()).hexdigest(),
                         '40ae8dd0753161be6a0a63ecf4dd4817813bcc0b8f342913cd041ce2b7645b31')
        identity = vision._cache_identity('a' * 64, 270,
            vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, 'fixture')
        self.assertEqual(identity['version'], 2)
        self.assertNotIn('wire_ids', identity)


class VisionResumeIdTests(unittest.TestCase):
    setUp = fixtures.VisionPipelineTests.setUp
    analyze = fixtures.VisionPipelineTests.analyze

    def test_fifty_two_original_cache_records_survive_failure_then_resume(self):
        self.probe.return_value['duration'] = 270
        planned, _ = vision._plan(self.asset['sha256'], 270)
        identity = vision._cache_identity(self.asset['sha256'], 270,
            self.options['endpoint'], self.options['model'], self.options['product_title'])
        records = {s['clip_id']: {'planned': s, 'observation': fixtures.observation(s['clip_id']),
            'quality': [{'mean': 100, 'black_ratio': 0}] * 3} for s in planned[:52]}
        path = vision._cache_path(self.cache, identity)
        vision._save_cache(path, identity, records)
        original = path.read_bytes()
        self.post.side_effect = lambda *args, **kwargs: fixtures.envelope([
            fixtures.observation('c1'), fixtures.observation('typo')])
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.response_issue, 'unknown_ids')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.post.call_count, 1)
        self.post.reset_mock()
        self.post.side_effect = fixtures.respond
        result = self.analyze()
        self.assertEqual(self.post.call_count, 19)
        self.assertEqual(len(result['scenes']), 90)
        cached = json.loads(path.read_text('utf-8'))['records']
        self.assertEqual(cached[:52], list(records.values()))
        self.assertEqual({r['observation']['clip_id'] for r in cached},
                         {s['clip_id'] for s in planned})


if __name__ == '__main__':
    unittest.main()

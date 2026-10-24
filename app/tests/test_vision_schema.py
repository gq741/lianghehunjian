"""Offline schema diagnostics: strict acceptance and zero response-content logging."""
import hashlib
import json
import unittest
from unittest.mock import patch

from mixstudio import providers, vision
import test_vision as fixtures


class VisionSchemaTests(unittest.TestCase):
    def decode(self, values):
        raw, content_type = fixtures.envelope(values)
        return vision._decode_response(raw, content_type, {'c1': 'a' * 24, 'c2': 'b' * 24})

    def schema_error(self, values, expected):
        with self.assertRaises(providers.ProviderError) as caught:
            self.decode(values)
        error = caught.exception
        self.assertEqual(error.response_issue, 'schema_invalid')
        self.assertEqual(error.schema_detail, expected)
        wrapped = vision.stage_error(vision._batch_error(error, 1, 20, 2), '画面识别 · 素材 3/3')
        self.assertEqual(wrapped.schema_detail, expected)
        self.assertEqual(wrapped.code, 'invalid_vision_response')
        self.assertTrue(wrapped.result_unknown)
        self.assertIn('未自动重试', str(wrapped))
        return wrapped

    def test_every_observation_rejection_reports_only_field_and_rule(self):
        cases = [
            ('description', None, 'text'), ('description', '', 'empty'),
            ('description', 'x' * 1001, 'length'), ('description', 'x\x00', 'control'),
            ('ocr', None, 'text'), ('ocr', 'x' * 1001, 'length'), ('ocr', 'x\x01', 'control'),
            ('reason', 3, 'text'), ('reason', '  ', 'empty'),
            ('reason', 'x' * 501, 'length'), ('reason', 'x\x02', 'control'),
            ('tags', None, 'array'), ('tags', ['a'] * 17, 'count'),
            ('tags', [3], 'text'), ('tags', [''], 'empty'),
            ('tags', ['x' * 81], 'length'), ('tags', ['x\x00'], 'control'),
            ('tags', ['a', ' a '], 'duplicate'),
            ('flags', None, 'array'), ('flags', ['black'] * 9, 'count'),
            ('flags', [3], 'text'), ('flags', [''], 'empty'),
            ('flags', ['x' * 81], 'length'), ('flags', ['x\x00'], 'control'),
            ('flags', ['black', 'black'], 'duplicate'), ('flags', ['invented'], 'enum'),
            ('confidence', '0.8', 'range'), ('confidence', True, 'range'),
            ('confidence', -0.1, 'range'), ('confidence', 1.1, 'range'),
            ('confidence', float('nan'), 'range'), ('confidence', float('inf'), 'range'),
            ('same_product', 'YES', 'enum'), ('same_product', True, 'enum'),
        ]
        for field, value, rule in cases:
            with self.subTest(field=field, rule=rule, kind=type(value).__name__):
                self.schema_error([fixtures.observation('c1'), fixtures.observation('c2', **{field: value})],
                                  (2, field, rule))

    def test_missing_known_fields_and_untrusted_extra_names_are_not_echoed(self):
        for field in ('description', 'ocr', 'reason', 'tags', 'flags', 'confidence', 'same_product'):
            item = fixtures.observation('c2')
            del item[field]
            with self.subTest(field=field):
                self.schema_error([fixtures.observation('c1'), item], (2, field, 'missing'))
        marker = 'credential-private-not-a-real-key'
        item = fixtures.observation('c2', **{marker: marker})
        error = self.schema_error([fixtures.observation('c1'), item], (2, 'observation', 'fields'))
        self.assertNotIn(marker, str(error))

    def test_return_item_index_not_assumed_input_order(self):
        error = self.schema_error([fixtures.observation('c2', confidence='0.8'), fixtures.observation('c1')],
                                 (1, 'confidence', 'range'))
        self.assertIn('返回第 1 项', str(error))
        self.assertIn('confidence', str(error))
        self.assertNotIn('c2', str(error))

    def test_envelope_and_container_rules_are_distinct(self):
        def envelope(data):
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}]}
        cases = [
            ([], (0, 'response', 'object')),
            ({}, (0, 'choices', 'single')),
            ({'choices': []}, (0, 'choices', 'single')),
            ({'choices': [1]}, (0, 'choices', 'single')),
            ({'choices': [{'finish_reason': 'other'}]}, (0, 'finish_reason', 'stop')),
            ({'choices': [{'finish_reason': 'stop'}]}, (0, 'message', 'object')),
            ({'choices': [{'finish_reason': 'stop', 'message': {}}]}, (0, 'content', 'text')),
            (envelope([]), (0, 'content', 'object')),
            (envelope({'extra': True}), (0, 'content', 'missing_observations')),
            (envelope({'observations': None}), (0, 'observations', 'array')),
            (envelope({'observations': [1]}), (1, 'observation', 'object')),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(providers.ProviderError) as caught:
                vision._decode_response(json.dumps(body).encode(), 'application/json', {'c1': 'a' * 24})
            self.assertEqual(caught.exception.schema_detail, expected)

    def test_forged_metadata_is_rejected_at_both_boundaries(self):
        marker = 'credential-private-not-a-real-key'
        forged = [None, marker, (2, 'confidence', marker), (2, marker, 'range'),
                  (True, 'confidence', 'range'), (3, 'confidence', 'range'),
                  (0, 'confidence', 'range'), (1, 'response', 'object'),
                  [2, 'confidence', 'range'], (2, 'confidence', 'range', marker),
                  (2, ['confidence'], 'range')]
        for value in forged:
            with self.subTest(value=str(value)):
                error = providers.ProviderError(marker, code='invalid_vision_response', result_unknown=True)
                error.response_issue = 'schema_invalid'
                error.schema_detail = value
                for result in (vision.stage_error(error, '画面识别'), vision._batch_error(error, 1, 20, 2)):
                    self.assertFalse(hasattr(result, 'schema_detail'))
                    self.assertNotIn(marker, str(result))
        error.schema_detail = (2, 'confidence', 'range')
        error.response_issue = 'unknown_ids'
        self.assertFalse(hasattr(vision.stage_error(error, '画面识别'), 'schema_detail'))

    def test_valid_values_and_prompt_identity_remain_unchanged(self):
        result = self.decode([fixtures.observation('c2', ocr='', tags=[], flags=[], confidence=0),
                              fixtures.observation('c1', confidence=1)])
        self.assertEqual([r['clip_id'] for r in result], ['b' * 24, 'a' * 24])
        self.assertEqual(result[0]['ocr'], '')
        self.assertEqual(hashlib.sha256(vision.SYSTEM_PROMPT.encode()).hexdigest(),
                         '40ae8dd0753161be6a0a63ecf4dd4817813bcc0b8f342913cd041ce2b7645b31')


class VisionSchemaCacheTests(unittest.TestCase):
    setUp = fixtures.VisionPipelineTests.setUp
    analyze = fixtures.VisionPipelineTests.analyze

    def test_invalid_second_batch_keeps_successful_cache_without_retry(self):
        self.probe.return_value['duration'] = 12
        def answer(*args, **kwargs):
            if self.post.call_count == 1:
                return fixtures.respond(*args, **kwargs)
            return fixtures.envelope([fixtures.observation('c1'), fixtures.observation('c2', confidence='0.8')])
        self.post.side_effect = answer
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.schema_detail, (2, 'confidence', 'range'))
        self.assertEqual(caught.exception.vision_batch, (2, 2, 2))
        self.assertEqual(self.post.call_count, 2)
        cache = next(self.cache.glob('vision-*.json'))
        records = json.loads(cache.read_text('utf-8'))['records']
        self.assertEqual(len(records), 2)
        original = cache.read_bytes()
        self.post.reset_mock()
        with self.assertRaises(providers.ProviderError):
            # One explicit new attempt receives the same invalid response.
            self.post.side_effect = lambda *args, **kwargs: fixtures.envelope([
                fixtures.observation('c1'), fixtures.observation('c2', confidence='0.8')])
            self.analyze()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(cache.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()

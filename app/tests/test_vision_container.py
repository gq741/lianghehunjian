"""Offline response-container regressions; no network or paid API calls.

CAPTURED_CONTENT came from the one authorized successful GLM batch replay on
2026-10-23. Its request/envelope identifiers are intentionally not included.
Every metadata, duplicate-key or invalid-field variant below is SYNTHETIC;
none is presented as the original failed response, which was not retained.
"""
import copy
import hashlib
import json
import unittest

from mixstudio import providers, vision
import test_vision as fixtures

CAPTURED_CONTENT = '{"observations":[{"clip_id":"c1","description":"白色立式空气净化器放在室内地面，人物用纸片在机器前演示吹/吸效果，随后特写机身圆形触控面板（有警示小字及“轻触接通机”类文字），最后人物蹲在一个印有滤芯图案的纸箱旁。字幕出现“吹”“吸”“等你的新房净化的差不多之后”。","tags":["外观","触控面板","演示"],"ocr":"吹 吸 等你的新房净化的差不多之后 轻触接通机","same_product":"yes","flags":[],"confidence":0.85,"reason":"白色塔式净化器外观与米家净化器6相符，演示吸吹风及操作面板，与标题商品明确相关"},{"clip_id":"c2","description":"人物用剪刀拆开印有滤芯图案的纸箱，取出用塑料袋包装的滤芯，特写滤芯标签“01 抗菌除敏芯”，手指指向标签。字幕：“等你的新房净化的差不多之后”“就可以直接换上这一套滤芯”“看 这是一个抗菌除敏滤芯”。","tags":["滤芯特写","开箱"],"ocr":"01 抗菌除敏芯 就可以直接换上这一套滤芯 看 这是一个抗菌除敏滤芯","same_product":"yes","flags":[],"confidence":0.85,"reason":"开箱展示配套滤芯，抗菌除敏滤芯与标题中双滤芯除醛的滤芯卖点相符"}]}'

WIRE_IDS = {'c1': 'a' * 24, 'c2': 'b' * 24}
MARKER = 'synthetic-private-metadata-do-not-display'


def captured():
    return json.loads(CAPTURED_CONTENT)


def envelope_content(content):
    return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'content': content}}]}, ensure_ascii=False).encode('utf-8')


def envelope(data):
    return envelope_content(json.dumps(data, ensure_ascii=False))


class VisionContainerTests(unittest.TestCase):
    def decode(self, data):
        return vision._decode_response(envelope(data), 'application/json', WIRE_IDS)

    def rejection(self, data, issue, detail=None):
        return self.raw_rejection(envelope(data), issue, detail)

    def raw_rejection(self, raw, issue, detail=None):
        with self.assertRaises(providers.ProviderError) as caught:
            vision._decode_response(raw, 'application/json', WIRE_IDS)
        error = caught.exception
        self.assertEqual(error.code, 'invalid_vision_response')
        self.assertEqual(error.response_issue, issue)
        self.assertTrue(error.result_unknown)
        if detail is not None:
            self.assertEqual(error.schema_detail, detail)
        wrapped = vision.stage_error(vision._batch_error(error, 8, 28, 2), '画面识别 · 素材 1/3')
        self.assertEqual(wrapped.response_issue, issue)
        self.assertNotIn(MARKER, str(error))
        self.assertNotIn(MARKER, str(wrapped))
        self.assertIn('未自动重试', str(wrapped))
        return error

    def test_captured_successful_response_preserves_all_observations(self):
        data = captured()
        result = self.decode(data)
        expected = [{**item, 'clip_id': WIRE_IDS[item['clip_id']]} for item in data['observations']]
        self.assertEqual(result, expected)

    def test_synthetic_scalar_top_level_metadata_does_not_reject_valid_content(self):
        expected = self.decode(captured())
        for value in (None, True, 2, 'metadata'):
            data = captured()
            data[MARKER] = value
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(self.decode(data), expected)

    def test_synthetic_nested_top_level_metadata_is_not_returned(self):
        data = captured()
        data[MARKER] = {'details': [MARKER], 'observations': [{'clip_id': 'invented'}]}
        result = self.decode(data)
        self.assertEqual(result, self.decode(captured()))
        self.assertNotIn(MARKER, json.dumps(result))

    def test_metadata_and_reordered_observations_use_exact_aliases(self):
        data = captured()
        data['observations'].reverse()
        data['summary'] = MARKER
        result = self.decode(data)
        self.assertEqual([x['clip_id'] for x in result], [WIRE_IDS['c2'], WIRE_IDS['c1']])
        self.assertEqual(result[0]['description'], data['observations'][0]['description'])

    def test_missing_observations_has_fixed_safe_diagnostic(self):
        for data in ({}, {MARKER: MARKER}):
            with self.subTest(keys=len(data)):
                self.rejection(data, 'schema_invalid', (0, 'content', 'missing_observations'))

    def test_data_and_results_wrappers_are_not_guessed(self):
        for key in ('data', 'results'):
            with self.subTest(wrapper=key):
                self.rejection({key: captured(), MARKER: MARKER}, 'schema_invalid',
                               (0, 'content', 'missing_observations'))

    def test_observations_requires_array_even_with_metadata(self):
        for value in (None, True, {}, '', 3):
            with self.subTest(kind=type(value).__name__):
                self.rejection({'observations': value, MARKER: MARKER}, 'schema_invalid',
                               (0, 'observations', 'array'))

    def test_nonobject_content_is_not_unwrapped(self):
        for value in ([captured()], None, CAPTURED_CONTENT):
            with self.subTest(kind=type(value).__name__):
                self.rejection(value, 'schema_invalid', (0, 'content', 'object'))

    def test_empty_observations_remains_missing_ids(self):
        self.rejection({'observations': [], MARKER: MARKER}, 'missing_ids')

    def test_incomplete_observations_remains_missing_ids(self):
        data = captured()
        data['observations'].pop()
        data['summary'] = MARKER
        self.rejection(data, 'missing_ids')

    def test_duplicate_clip_ids_are_not_deduplicated(self):
        data = captured()
        data['observations'][1]['clip_id'] = 'c1'
        data['summary'] = MARKER
        self.rejection(data, 'duplicate_ids')

    def test_unknown_clip_ids_are_not_remapped_by_position(self):
        data = captured()
        data['observations'][1]['clip_id'] = MARKER
        self.rejection(data, 'unknown_ids')

    def test_observation_extra_fields_still_reject_without_leaking_names(self):
        data = captured()
        data['observations'][1][MARKER] = MARKER
        data['summary'] = 'ignored'
        self.rejection(data, 'schema_invalid', (2, 'observation', 'fields'))

    def test_required_observation_fields_are_not_filled_from_metadata(self):
        for field in ('description', 'ocr', 'reason', 'tags', 'flags', 'confidence', 'same_product'):
            data = captured()
            data[field] = data['observations'][1].pop(field)
            with self.subTest(field=field):
                self.rejection(data, 'schema_invalid', (2, field, 'missing'))

    def test_internal_values_are_not_coerced_with_valid_metadata(self):
        for field, value, rule in (('confidence', '0.85', 'range'), ('confidence', True, 'range'),
                                   ('same_product', True, 'enum'), ('tags', '外观', 'array')):
            data = captured()
            data['observations'][0][field] = value
            data['summary'] = MARKER
            with self.subTest(field=field, value=value):
                self.rejection(data, 'schema_invalid', (1, field, rule))

    def test_malformed_content_remains_json_invalid(self):
        for content in ('{', CAPTURED_CONTENT[:-1], '```json\n' + CAPTURED_CONTENT + '\n```'):
            with self.subTest(length=len(content)):
                self.raw_rejection(envelope_content(content), 'json_invalid')

    def test_duplicate_envelope_choices_is_rejected_not_last_wins(self):
        valid = envelope(captured()).decode('utf-8')
        self.raw_rejection(('{"choices":[],' + valid[1:]).encode('utf-8'), 'json_invalid')

    def test_duplicate_message_content_is_rejected_not_last_wins(self):
        valid = envelope(captured()).decode('utf-8')
        raw = valid.replace('"content": ', '"content":"' + MARKER + '","content": ', 1)
        self.assertNotEqual(raw, valid)
        self.raw_rejection(raw.encode('utf-8'), 'json_invalid')

    def test_duplicate_observations_key_is_rejected_not_last_wins(self):
        duplicate = '{"observations":[],' + CAPTURED_CONTENT[1:]
        self.raw_rejection(envelope_content(duplicate), 'json_invalid')

    def test_duplicate_observation_fields_are_rejected_even_if_values_equal(self):
        for key, duplicate_value in (('confidence', '0.85'), ('clip_id', '"c1"')):
            content = CAPTURED_CONTENT.replace('"' + key + '":',
                '"' + key + '":' + duplicate_value + ',"' + key + '":', 1)
            with self.subTest(key=key):
                self.assertNotEqual(content, CAPTURED_CONTENT)
                self.raw_rejection(envelope_content(content), 'json_invalid')

    def test_duplicate_keys_inside_ignored_metadata_are_rejected(self):
        for raw in (
            envelope_content(CAPTURED_CONTENT[:-1] + ',"metadata":{"x":1,"x":2}}'),
            (envelope(captured()).decode('utf-8')[:-1] + ',"metadata":{"x":1,"x":2}}').encode('utf-8'),
        ):
            with self.subTest(length=len(raw)):
                self.raw_rejection(raw, 'json_invalid')

    def test_prompt_and_cache_algorithm_identity_are_unchanged(self):
        self.assertEqual(vision.ALGORITHM_VERSION, 2)
        self.assertEqual(hashlib.sha256(vision.SYSTEM_PROMPT.encode()).hexdigest(),
                         '40ae8dd0753161be6a0a63ecf4dd4817813bcc0b8f342913cd041ce2b7645b31')


class VisionContainerCacheTests(unittest.TestCase):
    setUp = fixtures.VisionPipelineTests.setUp
    analyze = fixtures.VisionPipelineTests.analyze

    def test_extra_metadata_never_reaches_cache_or_scene_and_cache_hit_is_free(self):
        data = captured()
        data[MARKER] = {'description': MARKER, 'clip_id': 'invented'}
        self.post.side_effect = lambda *args, **kwargs: (envelope(data), 'application/json')
        first = self.analyze()
        cache = next(self.cache.glob('vision-*.json'))
        self.assertNotIn(MARKER, cache.read_text('utf-8'))
        self.assertNotIn(MARKER, json.dumps(first))
        original = cache.read_bytes()
        frames = self.frame.call_count
        self.assertEqual(self.analyze(), first)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.frame.call_count, frames)
        self.assertEqual(cache.read_bytes(), original)

    def test_failed_second_batch_keeps_first_then_explicit_retry_resumes_once(self):
        self.probe.return_value['duration'] = 12
        data = captured()
        data[MARKER] = {'summary': MARKER}
        broken = copy.deepcopy(data)
        broken['observations'][1]['confidence'] = '0.85'
        self.post.side_effect = [(envelope(data), 'application/json'), (envelope(broken), 'application/json')]
        with self.assertRaises(providers.ProviderError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.response_issue, 'schema_invalid')
        self.assertEqual(caught.exception.schema_detail, (2, 'confidence', 'range'))
        self.assertEqual(self.post.call_count, 2)
        cache = next(self.cache.glob('vision-*.json'))
        before = json.loads(cache.read_text('utf-8'))
        self.assertEqual(len(before['records']), 2)
        self.assertNotIn(MARKER, cache.read_text('utf-8'))
        self.post.reset_mock()
        self.post.side_effect = lambda *args, **kwargs: (envelope(data), 'application/json')
        result = self.analyze()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(len(result['scenes']), 4)
        after = json.loads(cache.read_text('utf-8'))
        self.assertEqual(after['identity'], before['identity'])
        self.assertEqual(after['records'][:2], before['records'])
        self.assertEqual(len(after['records']), 4)
        self.assertNotIn(MARKER, cache.read_text('utf-8'))
        self.assertEqual(self.analyze(), result)
        self.assertEqual(self.post.call_count, 1)


if __name__ == '__main__':
    unittest.main()

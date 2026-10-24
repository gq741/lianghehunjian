"""Fixed-name script validation metadata; no model content enters diagnostics."""
import copy
import json
import unittest

from mixstudio import diagnostics, providers


class ScriptResponseDiagnosticTests(unittest.TestCase):
    def test_script_detail_allows_only_fixed_field_names_and_existing_rules(self):
        fields = ('response', 'drafts', 'draft', 'title', 'voiceover', 'segments',
                  'segment_id', 'text', 'visual_hint', 'fact_quotes')
        for field in fields:
            for index in (0, 1, 5):
                for rule in ('object', 'text', 'fields', 'array', 'identity',
                             'range', 'missing', 'length', 'empty', 'control',
                             'count', 'duplicate'):
                    with self.subTest(field=field, index=index, rule=rule):
                        value = {'script_detail': (index, field, rule)}
                        self.assertEqual(diagnostics.normalize_diagnostic(value),
                                         {'script_detail': [index, field, rule]})

    def test_script_counts_accepts_only_bounded_integer_counts(self):
        for expected in (1, 3, 5):
            for actual in (0, 1, 3, 10000):
                with self.subTest(expected=expected, actual=actual):
                    value = {'script_counts': (expected, actual)}
                    self.assertEqual(diagnostics.normalize_diagnostic(value),
                                     {'script_counts': [expected, actual]})

    def test_script_detail_rejects_malformed_or_unbounded_metadata(self):
        values = (None, {}, '1,title,text', [], [1], [1, 'title'],
                  [1, 'title', 'text', 'extra'], [True, 'title', 'text'],
                  [-1, 'title', 'text'], [6, 'title', 'text'],
                  [1.0, 'title', 'text'], ['1', 'title', 'text'],
                  [1, 'choices', 'text'], [1, 'private product title', 'text'],
                  [1, 'title', 'raw provider reply'], [1, None, 'text'],
                  [1, 'title', {}])
        for detail in values:
            with self.subTest(detail=detail):
                self.assertEqual(diagnostics.normalize_diagnostic(
                    {'code': 'invalid_response', 'script_detail': detail}),
                    {'code': 'invalid_response'})

    def test_script_counts_rejects_bool_coercion_wrong_shape_and_limits(self):
        values = (None, {}, '3,1', [], [1], [1, 1, 1], [0, 1], [6, 1],
                  [1, -1], [1, 10001], [True, 1], [1, False],
                  [1.0, 1], [1, 1.0], ['1', 1], [1, '1'])
        for counts in values:
            with self.subTest(counts=counts):
                self.assertEqual(diagnostics.normalize_diagnostic(
                    {'code': 'invalid_response', 'script_counts': counts}),
                    {'code': 'invalid_response'})

    def test_script_metadata_is_detached_json_safe_and_does_not_mutate_input(self):
        value = {'script_detail': [2, 'voiceover', 'empty'],
                 'script_counts': [3, 1]}
        before = copy.deepcopy(value)
        normalized = diagnostics.normalize_diagnostic(value)
        self.assertEqual(json.loads(json.dumps(normalized)), before)
        normalized['script_detail'][0] = 5
        normalized['script_counts'][0] = 5
        self.assertEqual(value, before)

    def test_provider_error_and_wrapper_preserve_only_approved_metadata(self):
        value = {'script_detail': [3, 'voiceover', 'text'],
                 'script_counts': [3, 3], 'response_issue': 'schema_invalid',
                 'response': 'PRIVATE MODEL RESPONSE', 'title': 'PRIVATE TITLE',
                 'voiceover': 'PRIVATE COPY', 'api_key': 'sk-PRIVATE',
                 'script_payload': {'raw': 'PRIVATE'}}
        source = providers.ProviderError('fixed local error', code='invalid_response',
                                         diagnostic=value)
        target = RuntimeError('wrapper')
        diagnostics.copy_diagnostic(source, target)
        expected = {'code': 'invalid_response', 'response_issue': 'schema_invalid',
                    'script_detail': [3, 'voiceover', 'text'],
                    'script_counts': [3, 3]}
        self.assertEqual(source.diagnostic, expected)
        self.assertEqual(target.diagnostic, expected)
        self.assertNotIn('PRIVATE', json.dumps(target.diagnostic))
        self.assertIsNot(source.diagnostic['script_detail'],
                         target.diagnostic['script_detail'])

    def test_script_diagnostics_do_not_expand_existing_vision_schema_contract(self):
        value = {'schema_detail': [2, 'description', 'missing'],
                 'vision_batch': [9, 18, 2],
                 'script_detail': [5, 'voiceover', 'empty']}
        self.assertEqual(diagnostics.normalize_diagnostic(value), value)
        for invalid in ([3, 'description', 'missing'],
                        [2, 'voiceover', 'empty']):
            self.assertEqual(diagnostics.normalize_diagnostic(
                {'schema_detail': invalid}), {})
        for invalid in ([1, 91, 2], [1, 90, 3]):
            self.assertEqual(diagnostics.normalize_diagnostic(
                {'vision_batch': invalid}), {})

    def test_unknown_keys_and_arbitrary_text_never_enter_script_metadata(self):
        value = {'script_detail': [0, 'drafts', 'count'],
                 'script_counts': [5, 0],
                 'raw_response': 'PRIVATE', 'raw_content': 'PRIVATE',
                 'script_reason': 'PRIVATE', 'request_payload': {'private': True}}
        self.assertEqual(diagnostics.normalize_diagnostic(value),
                         {'script_detail': [0, 'drafts', 'count'],
                          'script_counts': [5, 0]})


if __name__ == '__main__':
    unittest.main()

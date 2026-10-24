"""Soft reference timing: local estimates guide writing, not production gates."""
import copy
import json
import unittest
from unittest.mock import patch

from mixstudio import providers, script_engine
from mixstudio.v2 import duration_bounds, require_script_timing, validate_duration
import test_script_engine as fixtures


class SoftReferenceCopyTests(unittest.TestCase):
    def test_short_long_and_fractional_reference_copy_remains_ready_at_normal_speed(self):
        for reference, units in ((25, 2), (25, 79), (25, 122), (25, 181),
                                 (25.2, 80), (60, 10), (5, 240), (25, 400)):
            with self.subTest(reference=reference, units=units):
                value = script_engine.timing_fields('看' * units, reference)
                self.assertEqual(value['timing_status'], 'ready')
                self.assertEqual(value['timing_mode'], 'normal')
                self.assertEqual(value['estimated_voice_speed'], 1.)
                self.assertEqual(value['estimated_seconds'], units / 4)
                self.assertIn('参考', value['timing_message'])
                self.assertNotIn('至少删减', value['timing_message'])

    def test_reference_duration_is_not_an_acceptance_interval(self):
        for reference in (5, 25, 30, 60):
            self.assertEqual(duration_bounds(reference), (.1, 60))
            for actual in (.1, 1, 16.93, 19.99, 30.5, 59.9, 60):
                validate_duration(reference, actual)

    def test_actual_sixty_second_ceiling_and_invalid_inputs_remain(self):
        for actual in (0, .099, 60.001, True, '25', None, float('nan'), float('inf')):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                validate_duration(25, actual)
        for reference in (4, 61, True, '25', None, float('nan')):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                duration_bounds(reference)

    def test_stale_hard_gate_flags_are_recomputed_without_mutation(self):
        voice = '这款电视可以了解。'
        draft = {'voiceover': voice, 'segments': [{'text': voice}], 'target_seconds': 25,
                 'timing_status': 'needs_adjustment', 'estimated_voice_speed': 1.5}
        before = copy.deepcopy(draft)
        value = require_script_timing(draft)
        self.assertEqual(value['timing_status'], 'ready')
        self.assertEqual(value['estimated_voice_speed'], 1.)
        self.assertEqual(draft, before)

    def test_invalid_or_mismatched_copy_still_rejected(self):
        for text in ('', '。！？', 'a\x00', '看' * 2001):
            draft = {'voiceover': text, 'segments': [{'text': text}], 'target_seconds': 25}
            with self.subTest(text=text[:20]), self.assertRaises((ValueError, providers.ProviderError)):
                require_script_timing(draft)
        with self.assertRaisesRegex(ValueError, '不一致'):
            require_script_timing({'voiceover': '电视', 'segments': [{'text': '冰箱'}], 'target_seconds': 25})

    def test_generation_uses_one_existing_model_call_with_soft_writing_budget(self):
        with patch.object(providers, '_post', return_value=fixtures.wire(fixtures.draft_result())) as call:
            generated = script_engine.generate_scripts(fixtures.PRODUCT,
                api_key='test-placeholder', model=providers.DEFAULT_DEEPSEEK_MODEL,
                target_seconds=60, count=1)
        call.assert_called_once()
        self.assertEqual(generated[0]['timing_status'], 'ready')
        payload = call.call_args.args[1]
        data = json.loads(payload['messages'][1]['content'])
        self.assertEqual(data['timing_policy'], 'soft_reference')
        self.assertEqual(data['target_seconds'], 60)
        self.assertEqual(data['target_spoken_units'], 230)
        self.assertIn('软目标', payload['messages'][0]['content'])
        self.assertNotIn('计时字数必须在', payload['messages'][0]['content'])
        self.assertNotIn('每条须落入', payload['messages'][0]['content'])

    def test_content_claim_checks_not_removed_with_timing_gate(self):
        bad = fixtures.draft_result(fixtures.TEXT + '亲测效果最好。')
        with self.assertRaises(providers.ProviderError) as caught:
            script_engine._validate_scripts(bad, fixtures.PRODUCT['title'], 60, 1)
        self.assertEqual(caught.exception.code, 'unsupported_claim')


if __name__ == '__main__':
    unittest.main()

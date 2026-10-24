"""Reference estimates never force speech speed; local tests, no network."""
import unittest

from mixstudio import providers, script_engine
from mixstudio.v2 import require_script_timing, validate_duration


class SpeedTimingTests(unittest.TestCase):
    def fields(self, units, target=25):
        return script_engine.timing_fields('看' * units, target)

    def test_screenshot_122_units_can_continue_without_deleting_copy(self):
        result = self.fields(122)
        self.assertEqual(result['estimated_seconds'], 30.5)
        self.assertEqual(result['timing_status'], 'ready')
        self.assertEqual(result['timing_mode'], 'normal')
        self.assertEqual(result['estimated_voice_speed'], 1.)
        self.assertEqual(result['max_adaptable_spoken_units'], 360)
        self.assertIn('可继续匹配画面', result['timing_message'])
        self.assertIn('实测配音', result['timing_message'])
        self.assertNotIn('删减', result['timing_message'])

    def test_old_25_second_boundaries_are_soft(self):
        for units, status, mode in ((79, 'ready', 'normal'),
                (80, 'ready', 'normal'), (120, 'ready', 'normal'),
                (121, 'ready', 'normal'), (180, 'ready', 'normal'),
                (181, 'ready', 'normal')):
            with self.subTest(units=units):
                result = self.fields(units)
                self.assertEqual(result['timing_status'], status)
                self.assertEqual(result['timing_mode'], mode)
        self.assertEqual(self.fields(180)['estimated_voice_speed'], 1.)
        self.assertNotIn('至少删减', self.fields(181)['timing_message'])

    def test_over_sixty_estimate_does_not_block_copy_or_force_speed(self):
        for units, status in ((219, 'ready'), (220, 'ready'),
                (240, 'ready'), (360, 'ready'), (361, 'ready')):
            with self.subTest(units=units):
                result = self.fields(units, 60)
                self.assertEqual(result['timing_status'], status)
                self.assertEqual(result['max_adaptable_spoken_units'], 360)
        self.assertEqual(self.fields(360, 60)['estimated_voice_speed'], 1.)

    def test_fractional_target_keeps_guidance_but_not_a_speed_gate(self):
        result = self.fields(181, 25.2)
        self.assertEqual(result['timing_status'], 'ready')
        self.assertEqual(result['max_spoken_units'], 120)
        self.assertEqual(result['max_adaptable_spoken_units'], 360)
        self.assertEqual(result['estimated_voice_speed'], 1.)
        self.assertEqual(self.fields(182, 25.2)['timing_status'], 'ready')
        self.assertEqual(self.fields(80, 25.2)['timing_status'], 'ready')
        self.assertEqual(self.fields(81, 25.2)['timing_status'], 'ready')

    def test_generation_budget_remains_normal_not_max_speed_budget(self):
        self.assertEqual(script_engine.timing_budget(25),
            {'min_spoken_units': 80, 'max_spoken_units': 120, 'target_spoken_units': 100})
        self.assertEqual(script_engine.timing_budget(60),
            {'min_spoken_units': 220, 'max_spoken_units': 240, 'target_spoken_units': 230})

    def test_unicode_spoken_units_keep_punctuation_out_of_speed_estimate(self):
        voice = '中A2\U00020000Ⅲ²' * 20 + '看看，。！ \n👟'
        result = script_engine.timing_fields(voice, 25)
        self.assertEqual(result['spoken_units'], 122)
        self.assertEqual(result['timing_status'], 'ready')
        self.assertEqual(result['timing_mode'], 'normal')

    def test_stale_pending_draft_is_recomputed_without_mutation(self):
        voice = '看' * 122
        draft = {'voiceover': voice, 'target_seconds': 25,
            'segments': [{'segment_id': 's1', 'text': voice}],
            'timing_status': 'needs_adjustment', 'estimated_seconds': 99,
            'estimated_voice_speed': 3}
        result = require_script_timing(draft)
        self.assertEqual(result['timing_status'], 'ready')
        self.assertEqual(result['estimated_voice_speed'], 1.)
        self.assertEqual(draft['timing_status'], 'needs_adjustment')

    def test_forged_ready_estimate_cannot_bypass_empty_copy(self):
        for units in (0,):
            draft = {'voiceover': '看' * units, 'target_seconds': 25,
                'segments': [{'segment_id': 's1', 'text': '看' * units}],
                'timing_status': 'ready', 'timing_mode': 'normal',
                'estimated_seconds': 25, 'estimated_voice_speed': 1}
            with self.subTest(units=units), self.assertRaises(ValueError):
                require_script_timing(draft)

    def test_actual_finished_duration_only_keeps_positive_and_sixty_second_bounds(self):
        self.assertEqual(self.fields(122)['timing_status'], 'ready')
        for target, actual in ((25, 0), (25, .099), (60, 60.01)):
            with self.subTest(target=target, actual=actual), self.assertRaises(ValueError):
                validate_duration(target, actual)
        validate_duration(25, 30)
        validate_duration(25, 30.5)
        validate_duration(25, 19.99)
        validate_duration(60, 60)

    def test_invalid_targets_do_not_produce_ready_status(self):
        for target in (True, 4, 61, '25', None, float('nan'), float('inf')):
            with self.subTest(target=target), self.assertRaises(providers.ProviderError):
                self.fields(122, target)


if __name__ == '__main__':
    unittest.main()

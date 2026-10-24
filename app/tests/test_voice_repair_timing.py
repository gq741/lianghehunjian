"""Local-only regressions for incomplete ASR and sub-frame sentence boundaries."""
import copy
import random
import unittest

from mixstudio.voice_timing import align_segments, units


def segments(*lines):
    return [{'segment_id': f's{i}', 'text': text} for i, text in enumerate(lines, 1)]


def word(text, start, end):
    return {'word': text, 'start': start, 'end': end}


class VoiceRepairTimingTests(unittest.TestCase):
    def assert_valid(self, result, source, duration):
        self.assertEqual(len(result['segment_seconds']), len(source))
        self.assertAlmostEqual(sum(result['segment_seconds']), duration)
        self.assertTrue(all(value >= 0 for value in result['segment_seconds']))
        self.assertEqual(''.join(c['text'] for c in result['captions']), ''.join(s['text'] for s in source))
        self.assertEqual([sid for group in result['sentence_groups'] for sid in group],
                         [s['segment_id'] for s in source])
        previous = 0.
        for cue in result['captions']:
            self.assertAlmostEqual(cue['start'], previous)
            self.assertGreaterEqual(cue['end'] - cue['start'], .05 - 1e-9)
            previous = cue['end']
        self.assertAlmostEqual(previous, duration)

    def test_normal_measured_boundaries_stay_unchanged(self):
        source = segments('看看这款商品的外观。', '再了解它的细节。')
        result = align_segments(source, [word(source[0]['text'], .1, 2.5),
                                         word(source[1]['text'], 2.7, 4.8)], 5.)
        self.assertEqual(result['segment_seconds'], [2.7, 2.3])
        self.assertEqual(result['sentence_groups'], [['s1'], ['s2']])
        self.assert_valid(result, source, 5.)

    def test_high_coverage_missing_middle_sentence_uses_surrounding_word_edges(self):
        source = segments('甲乙丙丁。', '戊。', '己庚辛壬。')
        result = align_segments(source, [word('甲乙丙丁', 0., 1.), word('己庚辛壬', 2., 4.)], 4.)
        self.assertGreater(result['coverage'], .85)
        self.assertEqual(result['segment_seconds'], [1., 1., 2.])
        self.assertTrue(any('第2句' in warning and '估算' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_missing_sentence_prefix_is_estimated_inside_measured_gap(self):
        source = segments('甲乙丙丁。', '戊己庚辛。')
        result = align_segments(source, [word('甲乙丙丁', 0., 1.), word('己庚辛', 1.8, 4.)], 4.)
        self.assertAlmostEqual(result['captions'][1]['start'], 1.)
        self.assertTrue(any('估算' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_missing_final_short_sentence_uses_audio_end_without_new_speech(self):
        source = segments('甲乙丙丁戊己庚辛。', '壬。')
        result = align_segments(source, [word('甲乙丙丁戊己庚辛', 0., 3.)], 4.)
        self.assertEqual(result['segment_seconds'], [3., 1.])
        self.assertTrue(any('估算' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_moderate_coverage_does_not_invent_entire_missing_sentence(self):
        source = segments('甲乙丙。', '丁戊己。', '庚辛壬。')
        with self.assertRaisesRegex(ValueError, '没有取得可靠语音定位'):
            align_segments(source, [word('甲乙丙', 0., 1.), word('庚辛壬', 2., 4.)], 4.)

    def test_unrelated_transcript_still_fails_without_lowering_global_threshold(self):
        with self.assertRaisesRegex(ValueError, '对应不足'):
            align_segments(segments('商品支持导航和自动清洁。'), [word('完全不对应', 0., 4.)], 4.)

    def test_boundary_inside_one_asr_word_uses_that_word_interval(self):
        source = segments('甲乙。', '丙丁。')
        result = align_segments(source, [word('甲乙丙丁', 0., 4.)], 4.)
        self.assertEqual(result['segment_seconds'], [2., 2.])
        self.assertTrue(any('词内' in warning and '估算' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_thirty_millisecond_sentence_keeps_times_and_merges_caption_group(self):
        source = segments('甲。', '乙。', '丙。')
        result = align_segments(source, [word('甲', 0., 1.), word('乙', 1.5, 1.51),
                                         word('丙', 1.53, 4.)], 4.)
        self.assertAlmostEqual(result['segment_seconds'][1], .03)
        self.assertEqual(result['sentence_groups'], [['s1'], ['s2', 's3']])
        self.assertEqual(result['captions'][1]['text'], '乙。丙。')
        self.assertTrue(any('合并' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_final_thirty_milliseconds_merges_back_without_moving_boundary(self):
        source = segments('甲。', '乙。')
        result = align_segments(source, [word('甲', 0., 3.), word('乙', 3.97, 4.)], 4.)
        self.assertAlmostEqual(result['segment_seconds'][-1], .03)
        self.assertEqual(result['sentence_groups'], [['s1', 's2']])
        self.assert_valid(result, source, 4.)

    def test_final_zero_duration_sentence_is_not_dropped(self):
        source = segments('甲。', '乙。')
        result = align_segments(source, [word('甲', 0., 3.), word('乙', 4., 4.)], 4.)
        self.assertEqual(result['segment_seconds'], [4., 0.])
        self.assertEqual(result['sentence_groups'], [['s1', 's2']])
        self.assert_valid(result, source, 4.)

    def test_consecutive_zero_and_short_sentences_merge_to_valid_cues(self):
        source = segments('甲。', '乙。', '丙。', '丁。', '戊。')
        result = align_segments(source, [word('甲', 0., .5), word('乙', 1., 1.),
                                         word('丙', 1., 1.01), word('丁', 1.01, 1.02),
                                         word('戊', 1.02, 4.)], 4.)
        self.assertEqual(result['sentence_groups'], [['s1'], ['s2', 's3', 's4', 's5']])
        self.assert_valid(result, source, 4.)

    def test_one_sentence_does_not_need_an_interior_boundary(self):
        source = segments('只介绍这款商品。')
        result = align_segments(source, [word('只介绍这款商品', .2, 3.8)], 4.)
        self.assertEqual(result['segment_seconds'], [4.])
        self.assertEqual(result['sentence_groups'], [['s1']])
        self.assert_valid(result, source, 4.)

    def test_repeated_sentences_keep_separate_measured_word_anchors(self):
        source = segments('看看这款。', '看看这款。')
        result = align_segments(source, [word('看看这款', .1, 1.5), word('看看这款', 2., 4.)], 4.)
        self.assertEqual(result['segment_seconds'], [2., 2.])
        self.assert_valid(result, source, 4.)

    def test_nfkc_case_and_spacing_are_normalized_without_phonetic_claims(self):
        source = segments('ＲＥＤＭＩ １４４Ｈｚ。', 'Ａ２４ ２０２６款。')
        result = align_segments(source, [word('redmi 144 hz', .1, 1.5), word('a24 2026款', 2., 4.)], 4.)
        self.assertEqual(result['coverage'], 1.)
        self.assertEqual(result['segment_seconds'], [2., 2.])
        self.assertNotEqual(units('两千'), units('2000'))
        self.assert_valid(result, source, 4.)

    def test_spoken_number_difference_uses_local_gap_without_equating_numbers(self):
        source = segments('甲乙丙丁戊己。', '144赫兹刷新率。')
        result = align_segments(source, [word('甲乙丙丁戊己', 0., 1.),
                                         word('一百四十四', 1.2, 2.),
                                         word('赫兹刷新率', 2.5, 4.)], 4.)
        self.assertGreater(result['coverage'], .75)
        self.assertLess(result['coverage'], 1.)
        self.assertTrue(any('第2句' in warning and '估算' in warning for warning in result['warnings']))
        self.assert_valid(result, source, 4.)

    def test_dense_repeated_or_overlapping_word_times_preserve_all_text(self):
        rng = random.Random(2718)
        for count in (1, 2, 5, 16, 64):
            for duration in (.1, .2, 4., 60.):
                source = segments(*(chr(0x4e00+i)+'。' for i in range(count)))
                onsets = sorted(rng.choice([0., duration*.5, rng.random()*duration]) for _ in source)
                words = [word(part['text'], onset, min(duration, onset+duration*.1))
                         for part, onset in zip(source, onsets)]
                with self.subTest(count=count, duration=duration):
                    self.assert_valid(align_segments(source, words, duration), source, duration)

    def test_inputs_are_not_mutated(self):
        source = segments('甲。', '乙。')
        words = [word('甲乙', 0., 4.)]
        before = copy.deepcopy((source, words))
        align_segments(source, words, 4.)
        self.assertEqual((source, words), before)

    def test_bad_numeric_times_and_duration_remain_rejected(self):
        source = segments('甲。')
        for start, end in ((float('nan'), 1.), (-1., 1.), (2., 1.), (0., 5.)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                align_segments(source, [word('甲', start, end)], 4.)
        for duration in (True, 0., 60.001, float('nan'), float('inf')):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                align_segments(source, [word('甲', 0., .1)], duration)

    def test_bad_word_item_is_displayable_error(self):
        with self.assertRaises(ValueError):
            align_segments(segments('甲。'), [None], 4.)


if __name__ == '__main__':
    unittest.main()

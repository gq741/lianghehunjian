"""Pure duration conflicts must not be reported as missing footage."""
import unittest
from mixstudio.timeline import plan_timeline, shortage_message


def make_plan(seconds, *, phase='actual', target=30, with_scenes=True):
    scenes = [{'clip_id':'c1', 'source_path':'fixture.mp4', 'source_sha256':'a'*64,
               'start':0., 'end':120., 'usable':True}] if with_scenes else []
    return plan_timeline([{'segment_id':'s1', 'text':'看'*int(seconds*4)}], scenes,
                         [{'segment_id':'s1', 'clip_ids':['c1'] if with_scenes else []}],
                         target_seconds=target, transition_seconds=0.,
                         voice_seconds=[seconds] if phase=='actual' else None, phase=phase)


class TimingErrorReasonTests(unittest.TestCase):
    def test_real_short_voice_is_not_a_picture_shortage(self):
        plan = make_plan(16.904128)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['shortages'], [])
        self.assertEqual(plan['timing_constraint'], {'status':'ready','min_seconds':.1,'max_seconds':60.})
        self.assertEqual(plan['speed'],1.)
        self.assertAlmostEqual(plan['total_seconds'],16.904128)
        self.assertFalse(any('补充相关素材' in w or '重新匹配' in w for w in plan['warnings']))

    def test_long_voice_explains_speed_ceiling_and_real_lower_output(self):
        plan = make_plan(91.)
        self.assertEqual(plan['shortages'], [])
        self.assertEqual(plan['timing_constraint']['status'],'too_long')
        message = shortage_message(plan)
        for word in ('91.00','1.5','60.67','最高60','已有音频'):
            self.assertIn(word,message)
        self.assertNotIn('补充相关素材',message)

    def test_estimated_time_does_not_claim_cached_audio(self):
        plan = make_plan(16.75,phase='estimated')
        self.assertEqual(plan['status'],'ready')
        self.assertTrue(plan['warnings'])
        self.assertFalse(any('原始配音' in w or '已有音频' in w for w in plan['warnings']))

    def test_real_picture_shortage_remains_specific(self):
        plan = make_plan(27.,with_scenes=False)
        self.assertEqual(plan['timing_constraint']['status'],'ready')
        message = shortage_message(plan)
        self.assertIn('第1段缺',message)
        self.assertIn('补充相关素材',message)
        self.assertNotIn('低于',message)

    def test_mixed_time_and_picture_failure_keeps_both_reasons(self):
        plan = make_plan(91.,with_scenes=False)
        message = shortage_message(plan)
        self.assertIn('91.00',message)
        self.assertIn('最高60',message)
        self.assertIn('第1段缺',message)
        self.assertIn('补充相关素材',message)

    def test_approved_reference_change_does_not_relax_speed_or_duration_bounds(self):
        plan = make_plan(16.904128,target=17)
        self.assertEqual(plan['status'],'ready')
        self.assertEqual(plan['timing_constraint'],{'status':'ready','min_seconds':.1,'max_seconds':60.})
        self.assertEqual(plan['speed'],1.)
        too_long = make_plan(91.,target=60)
        self.assertEqual(too_long['status'],'shortage')
        self.assertEqual(too_long['speed'],1.5)
        self.assertEqual(too_long['timing_constraint']['max_seconds'],60.)
        self.assertIn('60.67',shortage_message(too_long))


if __name__ == '__main__':
    unittest.main()

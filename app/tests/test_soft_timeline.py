"""Reference duration is advisory; real narration/picture coverage drives edit."""
import unittest
from mixstudio.timeline import plan_timeline, reuse_stats


def scenes(*lengths):
    return [{'clip_id':f'c{i}','source_path':f'source{i}.mp4','source_sha256':str(i),
             'start':0.,'end':float(length),'usable':True} for i,length in enumerate(lengths)]


class SoftTimelineTests(unittest.TestCase):
    def plan(self,raw,footage,choices,target=25,fade=.2):
        return plan_timeline([{'segment_id':f's{i+1}','text':'看'*max(1,int(r*4))} for i,r in enumerate(raw)],
            footage,[{'segment_id':f's{i+1}','clip_ids':ids} for i,ids in enumerate(choices)],
            target_seconds=target,transition_seconds=fade,voice_seconds=raw,phase='actual')

    def test_short_voice_outside_reference_is_natural_not_rejected_or_padded(self):
        p=self.plan([16.904128],scenes(30),[['c0']],target=30)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['speed'],1.)
        self.assertAlmostEqual(p['total_seconds'],16.904128)
        self.assertEqual(p['video_speed'],1.)
        self.assertTrue(p['reference_is_soft'])

    def test_longer_than_reference_is_not_forced_faster(self):
        p=self.plan([35.],scenes(40),[['c0']],target=25)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['speed'],1.)
        self.assertEqual(p['total_seconds'],35.)
        self.assertTrue(any('参考' in warning for warning in p['warnings']))

    def test_picture_small_shortage_slows_picture_not_narration(self):
        p=self.plan([6.2,3.],scenes(3,5),[['c0'],['c1']],target=10)
        self.assertEqual(p['status'],'ready',p)
        self.assertEqual(p['speed'],1.)
        self.assertAlmostEqual(p['total_seconds'],9.2)
        self.assertGreaterEqual(p['segments'][0]['video_speed'],.85)
        self.assertLess(p['segments'][0]['video_speed'],1.)
        self.assertEqual(p['segments'][1]['video_speed'],1.)
        self.assertEqual(p['transition_seconds'],0.)
        self.assertLessEqual(p['repeat_count'],2)
        self.assertLessEqual(reuse_stats(p['windows'])['max_uses'],2)

    def test_playback_length_and_original_interval_remain_separate(self):
        p=self.plan([6.2],scenes(3),[['c0']],target=10)
        self.assertEqual(p['status'],'ready',p)
        self.assertTrue(any(w['video_speed']<1 for w in p['windows']))
        for w in p['windows']:
            self.assertTrue(w['source_start']<=w['start']<w['end']<=w['source_end']+1e-7)
            self.assertAlmostEqual(w['playback_seconds'],(w['end']-w['start'])/w['video_speed'])

    def test_long_picture_is_trimmed_without_slowing_it(self):
        p=self.plan([5.],scenes(40),[['c0']],target=30)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['video_speed'],1.)
        self.assertAlmostEqual(p['windows'][0]['end']-p['windows'][0]['start'],5.)

    def test_true_missing_footage_is_not_fabricated(self):
        p=self.plan([10.],scenes(.5),[['c0']],target=10)
        self.assertEqual(p['status'],'shortage')
        self.assertTrue(p['shortages'])
        self.assertLessEqual(p['speed'],1.5)
        self.assertGreaterEqual(p['video_speed'],.85)
        self.assertLessEqual(reuse_stats(p['windows'])['max_uses'],2)

    def test_delivery_cap_still_sixty_and_voice_never_above_one_point_five(self):
        p=self.plan([90.],scenes(100),[['c0']],target=25)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['speed'],1.5)
        self.assertEqual(p['total_seconds'],60.)
        q=self.plan([91.],scenes(100),[['c0']],target=25)
        self.assertEqual(q['status'],'shortage')
        self.assertEqual(q['timing_constraint']['status'],'too_long')


if __name__=='__main__':unittest.main()

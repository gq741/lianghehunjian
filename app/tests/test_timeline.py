import unittest
from mixstudio.timeline import plan_timeline, reuse_stats


def scene(cid, start=0, end=3, source=None):
    return dict(clip_id=cid, start=start, end=end, source_path=(source or cid)+'.mp4', source_sha256=source or cid, usable=True)

def segments(*seconds):
    return [dict(segment_id=f's{i+1}', text='看'*int(s*4)) for i,s in enumerate(seconds)]

def choices(*ids):
    return [dict(segment_id=f's{i+1}',clip_ids=x) for i,x in enumerate(ids)]

class TimelineTests(unittest.TestCase):
    def plan(self, segs, scenes, candidates, target=10, fade=.2, **kw):
        return plan_timeline(segs,scenes,candidates,target_seconds=target,transition_seconds=fade,**kw)
    def test_unique_before_repeat_and_speed(self):
        p=self.plan(segments(6),[scene(str(i)) for i in range(3)],choices(['0','1','2']))
        self.assertEqual(p['status'],'ready');self.assertEqual(p['speed'],1);self.assertEqual(p['repeat_count'],0)
        self.assertEqual(len(p['windows']),3)
    def test_two_extra_uses_and_per_window_twice(self):
        p=self.plan(segments(9),[scene('a'),scene('b')],choices(['a','b']))
        self.assertEqual(p['status'],'ready');self.assertEqual(p['speed'],1)
        self.assertEqual(p['repeat_count'],2);self.assertEqual(reuse_stats(p['windows'])['max_uses'],2)
    def test_minimum_global_speed_not_video_speed(self):
        p=self.plan(segments(8),[scene('a')],choices(['a']),target=8,fade=0)
        self.assertEqual(p['status'],'ready');self.assertAlmostEqual(p['speed'],8/(6/.85),places=5)
        self.assertAlmostEqual(p['video_speed'],.85,places=5)
        self.assertAlmostEqual(p['total_seconds'],6/.85,places=4)
        self.assertTrue(all(w['end']-w['start']<=3 for w in p['windows']))
    def test_all_shortages_reported_and_limit_respected(self):
        p=self.plan(segments(8,8),[scene('a',end=1),scene('b',end=1)],choices(['a'],['b']),target=16)
        self.assertEqual(p['status'],'shortage');self.assertEqual(len(p['shortages']),2)
        self.assertLessEqual(p['repeat_count'],2);self.assertLessEqual(p['speed'],1.5)
    def test_shared_candidate_reserved_for_scarce_sentence(self):
        p=self.plan(segments(2,2),[scene('a'),scene('b')],choices(['a','b'],['a']),target=5,fade=0)
        self.assertEqual(p['status'],'ready');self.assertEqual(p['repeat_count'],0)
    def test_alias_and_overlap_do_not_evade_reuse(self):
        windows=[dict(source_sha256='same',source_start=0,source_end=3),dict(source_sha256='same',source_start=1,source_end=4),dict(source_sha256='same',source_start=2,source_end=5)]
        self.assertEqual(reuse_stats(windows),{'repeat_count':2,'max_uses':3})
        p=self.plan(segments(8),[scene('a',source='same'),scene('b',source='same'),scene('c',source='same')],choices(['a','b','c']),target=8,fade=0)
        self.assertEqual(p['status'],'ready');self.assertEqual(len(p['windows']),2)
    def test_actual_replans_from_same_candidates(self):
        p=self.plan(segments(5),[scene('a'),scene('b')],choices(['a','b']),target=8,fade=0,voice_seconds=[8],phase='actual')
        self.assertEqual(p['phase'],'actual');self.assertEqual(p['status'],'ready');self.assertEqual(p['total_raw_seconds'],8)
    def test_reference_lower_bound_is_advisory_without_padding(self):
        p=self.plan(segments(3),[scene('a')],choices(['a']),target=25)
        self.assertEqual(p['status'],'ready');self.assertEqual(p['speed'],1)
        self.assertEqual(p['total_seconds'],3);self.assertTrue(p['warnings'])
    def test_transition_point_eight_uses_all_bound_candidates(self):
        sc=[scene(str(i),end=4) for i in range(6)]
        p=self.plan(segments(4,9.75,4),sc,choices(['0','1'],['1','2','3','4'],['5']),target=18,fade=.8,voice_seconds=[4,9.7,4],phase='actual')
        self.assertEqual(p['status'],'ready')
        for s in p['segments']:self.assertGreaterEqual(s['available_seconds']+.00001,s['required_seconds'])
    def test_speed_is_uniform_and_minimum_for_every_sentence(self):
        # s1: one3s window twice ->6; s2: two3s windows twice gives12,
        # but the global extra budget is only2, so its available total is9.
        p=self.plan(segments(8,8),[scene('a'),scene('b'),scene('c')],choices(['a'],['b','c']),target=16,fade=0)
        self.assertEqual(p['status'],'ready')
        self.assertAlmostEqual(p['speed'],8/(6/.85),places=5)
        self.assertEqual(p['repeat_count'],1)
        self.assertTrue(all(abs(row['raw_seconds']/row['voice_seconds']-p['speed'])<1e-7 for row in p['segments']))
        self.assertGreaterEqual(p['total_seconds'],11)
    def test_three_overlapping_aliases_cannot_buy_more_than_two_uses(self):
        sc=[scene('a',0,3,'same'),scene('b',.5,3.5,'same'),scene('c',1,4,'same')]
        p=self.plan(segments(12),sc,choices(['a','b','c']),target=10,fade=0)
        self.assertEqual(p['status'],'shortage')
        self.assertLessEqual(reuse_stats(p['windows'])['max_uses'],2)
    def test_raw_ninety_seconds_can_be_paced_to_sixty_but_not_ninety_one(self):
        sc=[scene(str(i),end=3) for i in range(30)]
        p=self.plan(segments(60),sc,choices([str(i) for i in range(30)]),target=60,fade=0,voice_seconds=[90],phase='actual')
        self.assertEqual(p['status'],'ready');self.assertEqual(p['speed'],1.5)
        p=self.plan(segments(60),sc,choices([str(i) for i in range(30)]),target=60,fade=0,voice_seconds=[91],phase='actual')
        self.assertEqual(p['status'],'shortage');self.assertEqual(p['speed'],1.5)

    def test_unknown_candidate_rejected_not_filled_with_unrelated_scene(self):
        with self.assertRaises(ValueError):self.plan(segments(5),[scene('a')],choices(['unknown']))
    def test_4500_candidates_considered_last_relevant_can_be_used(self):
        sc=[scene(str(i)) for i in range(4500)]
        p=self.plan(segments(3),sc,choices(['4499']),target=5,fade=0)
        self.assertEqual(p['windows'][0]['clip_id'],'4499')

if __name__=='__main__':unittest.main()

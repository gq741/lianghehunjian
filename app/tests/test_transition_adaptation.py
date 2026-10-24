"""Small overlap deficits adapt transitions, never source/reuse/voice limits."""
import copy
import unittest
from unittest.mock import patch

from mixstudio import providers
from mixstudio.timeline import plan_timeline, reuse_stats


def fixture():
    # The same 5 durations and 8 original windows as the reported 0.30s case.
    raw=[9.,7.,8.,5.25,6.]
    segments=[{'segment_id':f's{i+1}','text':'看'*int(s*4)} for i,s in enumerate(raw)]
    starts=[0,3,12,15,18,21,30,33]
    scenes=[{'clip_id':f'c{i}','source_path':'fixture.mp4','source_sha256':'same',
             'start':float(a),'end':float(a+3),'usable':True} for i,a in enumerate(starts)]
    pools=[[6,2],[1,4],[3,0],[6,5],[7,2]]
    candidates=[{'segment_id':f's{i+1}','clip_ids':[f'c{c}' for c in pool]} for i,pool in enumerate(pools)]
    return segments,scenes,candidates,raw


class TransitionAdaptationTests(unittest.TestCase):
    def plan(self, *, actual=False, fade=.2):
        segments,scenes,candidates,raw=fixture()
        return plan_timeline(segments,scenes,candidates,target_seconds=25,transition_seconds=fade,
                             voice_seconds=raw if actual else None,phase='actual' if actual else 'estimated')

    def assert_covered(self,plan):
        self.assertEqual(plan['status'],'ready',plan)
        self.assertEqual(plan['shortages'],[])
        self.assertTrue(all(row['available_seconds']+1e-7>=row['required_seconds'] for row in plan['segments']))
        self.assertLessEqual(plan['speed'],1.5)
        stats=reuse_stats(plan['windows'])
        self.assertLessEqual(stats['repeat_count'],2)
        self.assertLessEqual(stats['max_uses'],2)
        for window in plan['windows']:
            self.assertGreaterEqual(window['start'],window['source_start'])
            self.assertLessEqual(window['end'],window['source_end']+1e-7)
        self.assertTrue(.1<=plan['total_seconds']<=60)

    def test_reported_five_sentence_overlap_deficit_adapts_to_hard_cut(self):
        plan=self.plan()
        self.assert_covered(plan)
        self.assertEqual(plan['transition_seconds'],0.)
        self.assertEqual(plan['transition_adjustment']['requested_seconds'],.2)
        self.assertEqual(plan['transition_adjustment']['applied_seconds'],0.)
        self.assertAlmostEqual(plan['total_seconds'],sum(fixture()[3])/plan['speed'])
        self.assertTrue(any('直切' in w for w in plan['warnings']))

    def test_measured_and_estimated_use_same_adapted_plan(self):
        estimated=self.plan();actual=self.plan(actual=True)
        self.assert_covered(actual)
        self.assertEqual(actual['phase'],'actual')
        for key in ('speed','repeat_count','transition_seconds','segments','windows'):
            self.assertEqual(actual[key],estimated[key])

    def test_sufficient_footage_preserves_requested_fade(self):
        segments=[{'segment_id':'s1','text':'看'*40}]
        scenes=[{'clip_id':f'c{i}','source_path':f'{i}.mp4','start':0.,'end':4.,'usable':True} for i in range(4)]
        p=plan_timeline(segments,scenes,[{'segment_id':'s1','clip_ids':[s['clip_id'] for s in scenes]}],
                        target_seconds=10,transition_seconds=.2)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['transition_seconds'],.2)
        self.assertNotIn('transition_adjustment',p)

    def test_true_shortage_still_fails_without_more_source_or_repeat(self):
        p=plan_timeline([{'segment_id':'s1','text':'看'*48}],
            [{'clip_id':'c','source_path':'fixture.mp4','start':0.,'end':3.,'usable':True}],
            [{'segment_id':'s1','clip_ids':['c']}],target_seconds=10,transition_seconds=.2)
        self.assertEqual(p['status'],'shortage')
        self.assertTrue(p['shortages'])
        self.assertLessEqual(p['speed'],1.5)
        self.assertLessEqual(reuse_stats(p['windows'])['max_uses'],2)
        self.assertNotIn('transition_adjustment',p)

    def test_timing_conflict_does_not_become_ready_by_dropping_transitions(self):
        p=plan_timeline([{'segment_id':'s1','text':'看'*64}],
            [{'clip_id':'c','source_path':'fixture.mp4','start':0.,'end':40.,'usable':True}],
            [{'segment_id':'s1','clip_ids':['c']}],target_seconds=30,transition_seconds=.2,
            voice_seconds=[16.],phase='actual')
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['timing_constraint']['status'],'ready')
        self.assertEqual(p['speed'],1.)
        self.assertEqual(p['transition_seconds'],.2)
        self.assertNotIn('transition_adjustment',p)

    def test_explicit_hard_cut_does_not_claim_automatic_adjustment(self):
        p=self.plan(fade=0)
        self.assert_covered(p)
        self.assertNotIn('transition_adjustment',p)

    def test_no_paid_call_and_inputs_remain_unchanged(self):
        segs,scenes,candidates,_=fixture()
        before=copy.deepcopy((segs,scenes,candidates))
        with patch.object(providers,'_post',side_effect=AssertionError('No API for transition adaptation')):
            p=plan_timeline(segs,scenes,candidates,target_seconds=25,transition_seconds=.2)
        self.assert_covered(p)
        self.assertEqual((segs,scenes,candidates),before)


if __name__=='__main__':unittest.main()

"""Offline tests for global natural-speed assignment and honest shortage causes."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import shutil
import tempfile
import unittest
import wave

from mixstudio import media, timeline


def fixture(source='fixture.mp4',digest='one-original'):
    lengths=[3.,3.,.5,2.,1.,2.,1.,.5,1.,2.]
    pools=[[1,7,0],[9,3,5,2,0],[9,8,4,5,1],[1,2,6,8,7],[7,4,8,9]]
    scenes=[dict(clip_id=str(i),source_sha256=digest,source_path=str(source),
                 start=i*3.,end=i*3.+length,usable=True) for i,length in enumerate(lengths)]
    segments=[dict(segment_id=str(i),text='商品实拍') for i in range(5)]
    candidates=[dict(segment_id=str(i),clip_ids=[str(c) for c in ids]) for i,ids in enumerate(pools)]
    return segments,scenes,candidates,[6.,2.5,3.,1.5,3.]


def plan(segs,scenes,pools,durations,fade=0.):
    return timeline.plan_timeline(segs,scenes,pools,phase='actual',voice_seconds=durations,
                                  target_seconds=15,transition_seconds=fade,max_voice_speed=1.)


class GlobalPlanTests(unittest.TestCase):
    def test_swap_before_borrowing_repeat_quota(self):
        segs,scenes,candidates,raw=fixture()
        p=plan(segs,scenes,candidates,raw)
        self.assertEqual(p['status'],'ready');self.assertEqual(p['repeat_count'],0)
        self.assertEqual((p['speed'],p['video_speed']),(1.,1.))
        self.assertAlmostEqual(p['covered_seconds'],16.)
        for group,legal in zip(p['segments'],candidates):
            self.assertTrue(set(group['clip_ids']).issubset(legal['clip_ids']))
        self.assertGreater(p['search_nodes'],0)
        self.assertLessEqual(p['search_nodes'],timeline.SEARCH_NODES)

    def test_reassign_before_repeat_even_if_only_hard_cut_avoids_repeat(self):
        s=[dict(segment_id='s',text='商品')]
        c=[dict(clip_id=n,start=0.,end=3.,source_path=n+'.mp4',usable=True) for n in 'ab']
        p=plan(s,c,[dict(segment_id='s',clip_ids=['a','b'])],[6.],fade=.2)
        self.assertEqual((p['status'],p['repeat_count'],p['transition_seconds']),('ready',0,0.))

    def test_confirmed_empty_and_proven_insufficient_and_unresolved_are_distinct(self):
        segs,scenes,_,_=fixture()
        scenarios=[([],10.,'no_candidates'),(['0'],10.,'candidate_capacity'),(['0'],3.,'allocation_unresolved')]
        for ids,sec,code in scenarios:
            with self.subTest(code=code):
                # Three identical pools can only use the same original range twice.
                groups=segs if code=='allocation_unresolved' else segs[:1]
                p=plan(groups,scenes,[dict(segment_id=s['segment_id'],clip_ids=ids) for s in groups],[sec]*len(groups))
                self.assertEqual(p['status'],'shortage')
                self.assertTrue(all(s['reason_code']==code for s in p['shortages']))
                self.assertIn({'no_candidates':'没有已关联','candidate_capacity':'即使独占',
                               'allocation_unresolved':'现有分配未排齐'}[code],timeline.shortage_message(p))

    def test_same_source_aliases_do_not_raise_individual_capacity(self):
        segs,scenes,_,_=fixture()
        duplicate={**scenes[0],'clip_id':'alias','source_path':'renamed.mp4'}
        p=plan(segs[:1],[scenes[0],duplicate],[dict(segment_id='0',clip_ids=['0','alias'])],[9.])
        self.assertEqual(p['shortages'][0]['reason_code'],'candidate_capacity')
        self.assertLessEqual(timeline.reuse_stats(p['windows'])['max_uses'],2)

    def test_shared_budget_not_reset_across_stages(self):
        segs,scenes,candidates,raw=fixture()
        with patch.object(timeline,'SEARCH_NODES',7):
            p=plan(segs,scenes,candidates,raw,fade=.2)
        self.assertLessEqual(p['search_nodes'],7)
        self.assertLessEqual(timeline.reuse_stats(p['windows'])['repeat_count'],2)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'needs FFmpeg')
    def test_natural_zero_repeat_plan_exports_real_mp4(self):
        with tempfile.TemporaryDirectory(prefix='swap-flow-') as folder:
            root=Path(folder);source=root/'source.mp4';out=root/'output.mp4';audio=root/'utterance.wav'
            media._run(['ffmpeg','-v','error','-nostdin','-n','-f','lavfi','-i',
                'testsrc2=s=160x160:r=30:d=30','-c:v','libx264','-preset','ultrafast','-threads','1',source])
            sha=hashlib.sha256(source.read_bytes()).hexdigest();segs,scenes,candidates,raw=fixture(source,sha)
            p=plan(segs,scenes,candidates,raw);self.assertEqual(p['repeat_count'],0)
            with wave.open(str(audio),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\0\0'*round(sum(raw)*24000))
            by_id={s['clip_id']:s for s in scenes};clips=[];cues=[];cursor=0.
            for i,row in enumerate(p['segments']):
                ids=row['clip_ids'];clips.append({**by_id[ids[0]],'text':segs[i]['text'],
                    'additional_clips':[by_id[c] for c in ids[1:]]})
                cues.append(dict(start=cursor,end=cursor+raw[i],text=segs[i]['text']));cursor+=raw[i]
            result=media.render_video(clips,audio,out,dict(width=160,height=160,fps=30,
                transition='none',duration_seconds=cursor,timeline_plan=p,visual_unit_seconds=raw,
                subtitle_segments=cues,encoder='cpu',preset='fast'))
            self.assertTrue(result['published']);self.assertAlmostEqual(media.probe(out)['duration'],16.,delta=.05)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),sha)

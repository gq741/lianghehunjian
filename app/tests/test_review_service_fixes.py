"""Regression coverage for audit R02/R03/R05/R09. No real network/API."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from mixstudio import media, providers, script_engine, vision
from mixstudio.jobs import Cancelled
from mixstudio.service import Studio
from mixstudio.v2 import validate_duration
import test_v2_workflow as wf


class ReviewServiceContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mix-service-fixes-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.studio=Studio(self.root/'runtime')
        self.addCleanup(self.studio.close)
        self.net=patch.object(providers,'_http_open',side_effect=AssertionError('no network'))
        self.net.start();self.addCleanup(self.net.stop)

    def seed(self):
        source=self.root/'source.mp4';source.write_bytes(b'local fixture')
        self.studio.store.put_asset({'id':'a','path':str(source),'sha256':'a'*64,'duration':213,'imported_at':1,'name':'source.mp4'})
        scenes=[{'clip_id':f'c{i}','asset_id':'a','source_path':str(source),'start':3*i,'end':3*i+3,'usable':True,'text':'same product','tags':[],'confidence':1.,'reason':'fixture'} for i in range(71)]
        segments=[{'segment_id':'s1','text':'好'*50},{'segment_id':'s2','text':'好'*50}]
        draft={'id':'d1','workflow':'script_first_v2','voiceover':'好'*100,'segments':segments,'target_seconds':25,'product_name':'fixture','asset_ids':['a'],'revision':1,'created_at':1,'status':'draft','match_status':'ready','clips':scenes,'matches':[]}
        candidates=[{'segment_id':'s1','clip_ids':[f'c{i}' for i in range(65)]},{'segment_id':'s2','clip_ids':[f'c{i}' for i in range(65,71)]}]
        draft['match_candidates']=candidates
        self.studio._apply_timeline(draft,self.studio.settings.public())
        self.studio.store.put_draft(draft)
        return draft,candidates

    def test_match_public_status_preserves_draft_identity_not_payload(self):
        self.studio.store.create_job('m','match',{'draft_id':'d1','revision':7,'not_public':'private'})
        self.studio.store.update_job('m',status='running')
        job=next(j for j in self.studio.state()['jobs'] if j['id']=='m')
        self.assertEqual(job.get('draft_id'),'d1')
        self.assertNotIn('payload',job);self.assertNotIn('not_public',job)

    def test_manual_save_preserves_65_unedited_auto_candidates(self):
        _,candidates=self.seed()
        candidates[1]['clip_ids'].pop()
        result=self.studio.replace_matches_v2('d1',candidates)
        self.assertEqual(result['match_status'],'ready')
        self.assertEqual([len(m['clip_ids']) for m in result['match_candidates']],[65,5])

    def test_candidate_cap_and_source_validation_still_enforced(self):
        draft,candidates=self.seed()
        for bad in ([{'segment_id':'s1','clip_ids':['c0']*4501}],
                    [{'segment_id':'s1','clip_ids':['unknown']}],
                    [{'segment_id':'wrong','clip_ids':['c0']}]):
            with self.subTest(bad=bad[0]['segment_id']),self.assertRaises(ValueError):
                self.studio.replace_matches_v2('d1',bad)
        draft['clips'][0]['usable']=False
        self.studio.store.put_draft(draft)
        with self.assertRaises(ValueError):
            self.studio.replace_matches_v2('d1',[{'segment_id':'s1','clip_ids':['c0']}])

    def test_duration_validation_uses_soft_reference_and_sixty_second_cap(self):
        for actual in (30.01,19.99):
            validate_duration(25,actual)
        for target,actual in [(25,60.001),(25,.099),(60,60.01)]:
            with self.subTest(target=target,actual=actual),self.assertRaises(ValueError):
                validate_duration(target,actual)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class ReviewVoiceWorkflow(unittest.TestCase):
    def setUp(self):
        self.case=wf.V2WorkflowTests(methodName='test_script_first_real_25_second_export_uses_only_matched_windows_and_cached_voice')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.identifier=self.case.make_matched_draft()

    def assert_clean_work(self):
        self.assertEqual(list((self.case.studio.root/'work').iterdir()),[])
        self.assertEqual(self.case.studio.render_active,0)
        self.case.network.assert_not_called()

    def test_success_and_cached_rerender_cleanup_only_work_files(self):
        c=self.case
        first=c.wait(c.studio.render(self.identifier,True))
        self.assert_clean_work()
        cache={p.name:p.read_bytes() for p in (c.studio.root/'voice-cache').glob('*.wav')}
        second=c.wait(c.studio.render(self.identifier,True))
        self.assert_clean_work()
        self.assertTrue(Path(first['result']['path']).is_file())
        self.assertTrue(Path(second['result']['path']).is_file())
        self.assertEqual(len(c.voice_calls),2)
        self.assertEqual(cache,{p.name:p.read_bytes() for p in (c.studio.root/'voice-cache').glob('*.wav')})

    def test_failed_render_cleans_work_but_preserves_voice_cache(self):
        c=self.case
        c.renderer.side_effect=ValueError('local renderer fixture failure')
        failed=c.wait(c.studio.render(self.identifier,True),status='failed')
        self.assertIn('local renderer fixture failure',failed['error'])
        self.assertEqual(len(list((c.studio.root/'voice-cache').glob('*.wav'))),2)
        self.assert_clean_work()

    def test_cancelled_render_cleans_work(self):
        c=self.case
        def cancelled(*args,**kwargs):
            kwargs['cancel'].set()
            raise Cancelled('cancel test')
        c.renderer.side_effect=cancelled
        c.wait(c.studio.render(self.identifier,True),status='cancelled')
        self.assertEqual(len(list((c.studio.root/'voice-cache').glob('*.wav'))),2)
        self.assert_clean_work()

    def test_sample_rounding_preserves_voice_beyond_soft_reference(self):
        c=self.case
        samples=[187477,188076,199442,199259,239955,188553,194186]
        draft=c.studio.store.draft(self.identifier)
        assets=[c.studio.store.asset(aid) for aid in draft['asset_ids']]
        segments=[];clips=[];candidates=[]
        for i in range(7):
            sid=f's{i+1}';cid=f'probe-{i}';asset=assets[0 if i<4 else 1]
            start=i*4.8 if i<4 else (i-4)*6.6
            segments.append({'segment_id':sid,'text':'这是离线合成音频的测试文案'+str(i)+'。','visual_hint':'测试色块'})
            clips.append({'clip_id':cid,'asset_id':asset['id'],'source_path':asset['path'],'start':start,'end':start+(4.8 if i<4 else 6.6),'text':'合成色块','tags':['测试'],'usable':True,'confidence':.95,'reason':'离线'})
            candidates.append({'segment_id':sid,'clip_ids':[cid],'confidence':.95,'reason':'离线'})
        draft.update(segments=segments,voiceover=''.join(s['text'] for s in segments),clips=clips,matches=candidates,match_candidates=candidates,revision=draft['revision']+1,status='draft')
        draft.update(script_engine.timing_fields(draft['voiceover'],25))
        self.assertEqual(c.studio._apply_timeline(draft,c.studio.settings.public())['status'],'ready')
        c.studio.store.put_draft(draft)
        calls=[]
        def voice(text,output,**kwargs):
            n=samples[len(calls)];calls.append(n)
            with wave.open(str(output),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(44100);out.writeframes(b'\x01\x00'*n)
            return {'path':str(output),'duration':n/44100}
        c.voice.side_effect=voice
        job=c.wait(c.studio.render(self.identifier,True))
        # Reference25 is not a30-second ceiling anymore: retain the complete
        # measured voice instead of accelerating it merely to fit that range.
        expected=sum(samples)/44100
        self.assertAlmostEqual(job['result']['voice_duration'],expected,delta=9/48000)
        self.assertEqual(job['result']['voice_speed'],1.)
        info=media.probe(Path(job['result']['path']))
        self.assertAlmostEqual(info['duration'],expected,delta=1/c.studio.settings.public()['fps']+.001)
        self.assertLessEqual(info['duration'],60.)
        self.assertEqual(len(calls),7)
        self.assertEqual(len(list((c.studio.root/'voice-cache').glob('*.wav'))),7)
        self.assert_clean_work()


if __name__=='__main__':unittest.main()

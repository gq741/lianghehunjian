import unittest
from mixstudio.timeline import plan_timeline, reuse_stats, shortage_message


class TimelineUpdateTests(unittest.TestCase):
    def fixture(self):
        lengths = [1., .6, 3., .6, 3., 3., 1., 1., 3., 3.]
        pools = [[7, 2, 1], [1, 4, 3, 0, 5], [2, 5], [9, 6, 5, 8, 1], [0, 3, 4, 2, 9]]
        raw = [7.5, 7.5, 3., 4.5, 3.]
        scenes = [dict(clip_id=str(i), start=0, end=d, source_path=f'{i}.mp4',
                       source_sha256=str(i), usable=True) for i, d in enumerate(lengths)]
        segments = [dict(segment_id=f's{i}', text='测试口播') for i in range(5)]
        candidates = [dict(segment_id=f's{i}', clip_ids=list(map(str, p))) for i, p in enumerate(pools)]
        return segments, scenes, candidates, raw

    def test_reallocate_instead_of_false_shortage(self):
        segments, scenes, candidates, raw = self.fixture()
        plan = plan_timeline(segments, scenes, candidates, target_seconds=30,
                             transition_seconds=.2, voice_seconds=raw, phase='actual', max_voice_speed=1.)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['speed'], 1.)
        self.assertLessEqual(plan['repeat_count'], 2)
        self.assertLessEqual(reuse_stats(plan['windows'])['max_uses'], 2)
        for row, candidate in zip(plan['segments'], candidates):
            self.assertLess(row['missing_seconds'], .00001)
            self.assertTrue(set(row['clip_ids']).issubset(candidate['clip_ids']))
        self.assertAlmostEqual(plan['covered_seconds'], sum(raw), places=5)

    def test_real_shortage_is_not_hidden_and_reports_actual_coverage(self):
        plan = plan_timeline([dict(segment_id='a', text='商品')],
            [dict(clip_id='x', start=0, end=.5, source_path='x.mp4', usable=True)],
            [dict(segment_id='a', clip_ids=['x'])], target_seconds=30,
            transition_seconds=.2, voice_seconds=[4.69], phase='actual', max_voice_speed=1.)
        self.assertEqual(plan['status'], 'shortage')
        self.assertAlmostEqual(plan['total_seconds'], 4.69)
        self.assertAlmostEqual(plan['covered_seconds'], 1/.85)
        self.assertNotIn('当前真实画面仍不足', shortage_message(plan))

    def test_empty_pool_is_not_filled_from_unrelated_material(self):
        segments, scenes, candidates, raw = self.fixture()
        candidates[4]['clip_ids'] = []
        plan = plan_timeline(segments, scenes, candidates, target_seconds=30,
            transition_seconds=0, voice_seconds=raw, phase='actual', max_voice_speed=1.)
        self.assertEqual(plan['status'], 'shortage')
        self.assertEqual(plan['segments'][4]['clip_ids'], [])
        self.assertLessEqual(plan.get('search_nodes', 0), 12000)

    def test_anonymous_replay_survives_job_log_normalization(self):
        from mixstudio.timeline import TimelineShortage
        from mixstudio.diagnostics import normalize_diagnostic
        segments, scenes, candidates, raw = self.fixture()
        candidates[4]['clip_ids'] = []
        plan = plan_timeline(segments, scenes, candidates, target_seconds=30,
            transition_seconds=.2, voice_seconds=raw, phase='actual', max_voice_speed=1.)
        detail = normalize_diagnostic(TimelineShortage(plan).diagnostic)
        self.assertEqual(detail['code'], 'timeline_shortage')
        case = detail['timeline_case']
        import json
        self.assertNotIn('.mp4', json.dumps(case))
        self.assertNotIn('测试口播', json.dumps(case, ensure_ascii=False))
        self.assertEqual(case['raw'], raw)
        dirty = {**case, 'secret': 'do-not-log'}
        self.assertNotIn('timeline_case', normalize_diagnostic({'timeline_case': dirty}))

    def test_replay_can_reproduce_without_original_paths_or_text(self):
        from mixstudio.timeline import TimelineShortage
        from mixstudio.diagnostics import normalize_diagnostic
        segments, scenes, candidates, raw = self.fixture()
        candidates[4]['clip_ids'] = []
        original = plan_timeline(segments,scenes,candidates,target_seconds=30,
            transition_seconds=.2,voice_seconds=raw,phase='actual',max_voice_speed=1.)
        case = normalize_diagnostic(TimelineShortage(original).diagnostic)['timeline_case']
        restored = plan_timeline([{'segment_id':str(i),'text':''} for i in range(len(case['raw']))],
            [dict(clip_id=str(i), source_sha256=str(r[0]),source_path=f'fixture-{r[0]}.mp4',
                  start=r[1],end=r[2],usable=True) for i,r in enumerate(case['scenes'])],
            [dict(segment_id=str(i),clip_ids=list(map(str,p))) for i,p in enumerate(case['pools'])],
            target_seconds=case['target'],transition_seconds=case['transition'],
            voice_seconds=case['raw'],phase=case['phase'],max_voice_speed=case['max_voice_speed'])
        self.assertEqual([s['missing_seconds'] for s in original['segments']],
                         [s['missing_seconds'] for s in restored['segments']])

    def test_source_aliases_still_cannot_evade_repeat_limits(self):
        scenes=[dict(clip_id=str(i),source_path=f'renamed-{i}.mp4',source_sha256='same',
                     start=0.,end=3.,usable=True) for i in range(3)]
        plan=plan_timeline([dict(segment_id=str(i),text='商品') for i in range(3)],scenes,
            [dict(segment_id=str(i),clip_ids=[str(i)]) for i in range(3)],target_seconds=9,
            transition_seconds=0,voice_seconds=[3.,3.,3.],phase='actual',max_voice_speed=1.)
        self.assertEqual(plan['status'],'shortage')
        self.assertLessEqual(reuse_stats(plan['windows'])['max_uses'],2)


class RealReallocationTest(unittest.TestCase):
    def test_counterexample_exports_real_video_without_paid_calls(self):
        import tempfile, pathlib, hashlib, wave, shutil
        from mixstudio import media
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg is required')
        segments, scenes, candidates, raw = TimelineUpdateTests().fixture()
        with tempfile.TemporaryDirectory(prefix='reallocate-real-') as folder:
            root=pathlib.Path(folder)
            for i,scene in enumerate(scenes):
                path=root/f'{i}.mp4'
                media._run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-f','lavfi','-i',
                    f'color=c=0x{20+i*13:02x}{40+i*7:02x}{80+i*3:02x}:s=320x480:r=30:d={scene["end"]}',
                    '-c:v','libx264','-preset','ultrafast','-threads','1',str(path)])
                scene.update(source_path=str(path),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            before={s['source_path']:s['source_sha256'] for s in scenes}
            voice=root/'whole.wav'
            with wave.open(str(voice),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
                audio.writeframes(b'\0\0'*int(24000*sum(raw)))
            plan=plan_timeline(segments,scenes,candidates,target_seconds=30,
                transition_seconds=.2,voice_seconds=raw,phase='actual',max_voice_speed=1.)
            self.assertEqual(plan['status'],'ready')
            index={s['clip_id']:s for s in scenes};clips=[];captions=[];start=0.
            for row,seconds in zip(plan['segments'],raw):
                ids=list(dict.fromkeys(row['clip_ids']))
                clips.append({**index[ids[0]],'text':'测试画面',
                    'additional_clips':[dict(index[c]) for c in ids[1:]]})
                captions.append(dict(start=start,end=start+seconds,text='测试画面'));start+=seconds
            output=root/'out.mp4'
            result=media.render_video(clips,voice,output,dict(width=320,height=480,fps=30,
                encoder='cpu',preset='fast',duration_seconds=sum(raw),timeline_plan=plan,
                visual_unit_seconds=raw,transition='fade',transition_seconds=.2,subtitle_segments=captions))
            self.assertTrue(result['published'])
            self.assertAlmostEqual(media.probe(output)['duration'],sum(raw),delta=.08)
            self.assertTrue(all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in before.items()))


if __name__ == '__main__':
    unittest.main()

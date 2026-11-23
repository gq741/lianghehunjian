"""Bounded resume permissions tested with local WAVs, SQLite and actual FFmpeg."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from mixstudio import providers, script_engine, vision
from mixstudio.whole_voice import _audio_key
import test_whole_voice as fx


class ResumeTests(unittest.TestCase):
    setUpClass = classmethod(fx.ProductionTests.setUpClass.__func__)
    tearDownClass = classmethod(fx.ProductionTests.tearDownClass.__func__)
    def setUp(self):
        fx.ProductionTests.setUp(self)
        self.draft['segments'] = [dict(s,visual_hint='商品外观') for s in self.draft['segments']]
        self.studio.store.put_draft(self.draft)

    tearDown = fx.ProductionTests.tearDown
    wait = fx.ProductionTests.wait
    voice = fx.ProductionTests.voice
    timing = fx.ProductionTests.timing
    scenes = fx.ProductionTests.scenes
    matching = fx.ProductionTests.matching
    patches = fx.ProductionTests.patches

    def paid_success(self):
        with self.patches():
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'completed',job.get('error'))
        self.calls.clear()
        return self.studio.store.draft('d')

    def preview(self,retry=False):
        return self.studio.production_budget('d',retry=retry)

    def test_cached_local_only_renders_without_asr_paid_calls_or_provider_keys(self):
        self.paid_success()
        with patch.object(self.studio.settings,'secret',side_effect=AssertionError('read unused key')), \
             patch('mixstudio.whole_voice.synthesize_speech',side_effect=AssertionError('paid')), \
             patch.object(self.studio,'_align_whole',side_effect=AssertionError('realignment')), \
             patch.object(self.studio,'_match_operation',side_effect=AssertionError('rematch')), \
             patch('mixstudio.providers._run_process',return_value=(0,'')), \
             patch('mixstudio.subtitle_regions.locate',return_value=[]):
            budget=self.preview()
            self.assertTrue(budget['local_only'])
            self.assertEqual(budget['paid_steps'],[])
            self.assertEqual(budget['alignment_state'],'cached')
            result=self.wait(self.studio.produce_whole('d',False,expected_budget=budget['token']))
        self.assertEqual(result['status'],'completed',result.get('error'))

    def test_complete_vision_needs_semantics_only_and_solves_plan_once(self):
        d=self.paid_success()
        d.update(match_candidates=[],match_status='pending',match_origin='auto')
        self.studio.store.put_draft(d)
        with self.patches(),patch.object(vision,'analyze_asset',side_effect=AssertionError('repeated GLM')), \
             patch.object(self.studio,'_apply_timeline',wraps=self.studio._apply_timeline) as plan:
            b=self.preview()
            self.assertEqual(b['paid_steps'],['deepseek'])
            with self.assertRaises(ValueError):self.studio.produce_whole('d',False,expected_budget=b['token'])
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=b['token']))
        self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertEqual([c[0] for c in self.calls if c[0]!='preflight'],['deepseek'])
        self.assertEqual(plan.call_count,1)

    def test_cached_match_only_needs_new_audio(self):
        d=self.paid_success()
        self.studio.settings.update({'voice_provider':'minimax'})
        b=self.preview()
        self.assertEqual(b['paid_steps'],['minimax']);self.assertEqual(b['matching_state'],'cached')
        with self.patches(),patch('mixstudio.whole_voice.synthesize_speech',side_effect=self.voice), \
             patch.object(self.studio,'_match_operation',side_effect=AssertionError('rematched')):
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=b['token']))
        self.assertEqual(r['status'],'completed',r.get('error'))

    def test_cache_lost_after_confirmation_cannot_charge_even_for_allow_paid_true(self):
        d=self.paid_success();b=self.preview()
        def preflight(*a,**kw):Path(d['whole_audio']['path']).unlink()
        with self.patches(),patch.object(self.studio,'_free_preflight',side_effect=preflight):
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=b['token']))
        self.assertEqual(r['status'],'failed')
        self.assertFalse(self.calls)
        b=self.preview()
        self.assertEqual(b['voice_state'],'missing')
        self.assertTrue(b['blocked'])
        self.assertFalse(self.preview(retry=True)['blocked'])

    def test_pending_retry_budget_bound_to_exact_attempt(self):
        settings=self.studio.settings.public();k=_audio_key(self.draft,settings)
        mark=self.studio.root/'voice-cache'/('whole-v1-'+k+'.pending.json')
        mark.write_text(json.dumps({'attempt':'first','state':'pending'}),'utf8')
        self.assertTrue(self.preview()['blocked'])
        b=self.preview(True)
        mark.write_text(json.dumps({'attempt':'second','state':'pending'}),'utf8')
        with self.patches(),self.assertRaisesRegex(ValueError,'确认'):
            self.studio.produce_whole('d',True,retry_unknown=True,expected_budget=b['token'])
        self.assertFalse(self.calls)

    def test_corrupt_aligned_captions_force_local_asr_not_pay(self):
        d=self.paid_success();d['whole_voice']['captions'][0]['text']='其他商品'
        self.studio.store.put_draft(d); b=self.preview()
        self.assertTrue(b['local_only']);self.assertEqual(b['alignment_state'],'local')
        with self.patches():
            r=self.wait(self.studio.produce_whole('d',False,expected_budget=b['token']))
        self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertEqual([c[0] for c in self.calls if c[0] not in ('preflight','alignment')],[])
        self.assertIn(('alignment',5.),self.calls)


    def test_known_missing_after_completed_job_can_explicitly_replace_once_and_keep_matches(self):
        d=self.paid_success()
        wav=Path(d['whole_audio']['path']);wav.unlink()
        self.assertFalse(wav.with_suffix('.pending.json').is_file())  # preview is read-only
        b=self.preview(retry=True)
        self.assertEqual(b['paid_steps'],['fish'])
        self.assertFalse(wav.with_suffix('.pending.json').is_file())
        with self.patches():
            for consent, token in [(True, None),(False,b['token']),(True,self.preview()['token'])]:
                with self.assertRaises(ValueError):
                    self.studio.produce_whole('d', consent, expected_budget=token, replace_missing_audio=True)
            r=self.wait(self.studio.produce_whole('d', True, expected_budget=b['token'], replace_missing_audio=True))
        self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertEqual([c[0] for c in self.calls if c[0] in ('fish','glm','deepseek')],['fish'])
        self.assertTrue(wav.is_file())

    def test_replace_missing_rejects_unknown_and_disappeared_retry_does_not_rebuy(self):
        k=_audio_key(self.draft,self.studio.settings.public())
        mark=self.studio.root/'voice-cache'/('whole-v1-'+k+'.pending.json')
        mark.write_text(json.dumps({'attempt':'old','state':'pending'}),'utf8')
        b=self.preview(True)
        with self.assertRaisesRegex(ValueError, '缺失'):
            self.studio.produce_whole('d',True, expected_budget=b['token'], replace_missing_audio=True)
        with self.patches(),patch.object(self.studio,'_free_preflight',side_effect=lambda *a,**kw:mark.unlink()):
            r=self.wait(self.studio.produce_whole('d',True, expected_budget=b['token'], retry_unknown=True))
        self.assertEqual(r['status'],'failed')
        self.assertFalse(self.calls)

    def test_running_semantic_only_budget_loses_voice_zero_paid_calls(self):
        d=self.paid_success();d.update(match_candidates=[],match_status='pending',match_origin='auto')
        self.studio.store.put_draft(d)
        b=self.preview();self.assertEqual(b['paid_steps'],['deepseek'])
        def preflight(*args,**k):Path(d['whole_audio']['path']).unlink()
        with self.patches(),patch.object(self.studio,'_free_preflight',side_effect=preflight):
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=b['token']))
        self.assertEqual(r['status'],'failed')
        self.assertFalse(self.calls)


    def interrupted_semantic(self):
        d=self.paid_success()
        d.update(matches=[],match_candidates=[],match_status='pending',match_origin='auto')
        self.studio.store.put_draft(d)
        result={'candidates':[{'segment_id':s['segment_id'],'clip_ids':['c'],'confidence':.95,
                               'reason':'产品外观'} for s in d['segments']], 'unmatched':[]}
        cache=self.studio.root/'semantic-cache'
        with patch.object(script_engine,'_complete',return_value=result):
            script_engine.match_segments(d['segments'],d['clips'],api_key='offline-placeholder',
                model=self.studio.settings.public()['deepseek_model'],product_title=d['product_name'],
                cache_dir=cache,cache_context={'draft_id':d['id'],'revision':d['revision'],
                    'sources':[{'id':'a','sha256':self.asset['sha256']}]})
        return next(cache.glob('semantic-*.json'))

    def test_all_semantic_checkpoints_merge_without_key_or_network(self):
        self.interrupted_semantic()
        with patch.object(self.studio.settings,'secret',side_effect=AssertionError('read unused key')), \
             patch.object(script_engine,'_complete',side_effect=AssertionError('DeepSeek')), \
             patch.object(vision,'analyze_asset',side_effect=AssertionError('GLM')), \
             patch('mixstudio.providers._run_process',return_value=(0,'')), \
             patch('mixstudio.subtitle_regions.locate',return_value=[]):
            b=self.preview()
            self.assertTrue(b['local_only'])
            r=self.wait(self.studio.produce_whole('d',False,expected_budget=b['token']))
        self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertTrue(self.studio.store.draft('d')['match_candidates'])

    def test_semantic_checkpoint_lost_during_free_resume_never_buys(self):
        checkpoint=self.interrupted_semantic()
        b=self.preview();self.assertTrue(b['local_only'])
        with patch.object(self.studio,'_free_preflight',side_effect=lambda *a,**k:checkpoint.unlink()), \
             patch.object(script_engine,'_complete',side_effect=AssertionError('bought')) as paid, \
             patch.object(self.studio.settings,'secret',side_effect=AssertionError('key')), \
             patch.object(vision,'analyze_asset',side_effect=AssertionError('GLM')):
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=b['token']))
        paid.assert_not_called()
        self.assertEqual(r['status'],'failed')
        self.assertIn('确认',r['error'])
        self.assertEqual(self.preview()['paid_steps'],['deepseek'])

    def test_partial_quote_does_not_rebuy_a_previously_cached_semantic_batch(self):
        d=self.paid_success()
        d.update(matches=[],match_candidates=[],match_status='pending',match_origin='auto',
                 clips=[dict(d['clips'][0],clip_id=f'c{i}') for i in range(129)])
        self.studio.store.put_draft(d)
        k=self.studio.settings.public()['deepseek_model'];cache=self.studio.root/'semantic-cache'
        context={'draft_id':'d','revision':d['revision'],'sources':[{'id':'a','sha256':self.asset['sha256']}]}
        answer={'candidates':[], 'unmatched':[s['segment_id'] for s in d['segments']]}
        # First 128 scenes committed; last batch interrupted, not submitted again.
        with patch.object(script_engine,'_complete',side_effect=[answer,RuntimeError('simulated stop')]), self.assertRaises(RuntimeError):
            script_engine.match_segments(d['segments'],d['clips'],api_key='offline-placeholder',
                model=k,product_title=d['product_name'],cache_dir=cache,cache_context=context)
        checkpoint=next(cache.glob('semantic-*.json'))
        budget=self.preview()
        self.assertEqual((budget['semantic_cached_batches'],budget['semantic_new_requests']),(1,1))
        with patch.object(self.studio,'_free_preflight',side_effect=lambda *a,**k:checkpoint.unlink()), \
             patch.object(script_engine,'_complete',return_value=answer) as paid:
            r=self.wait(self.studio.produce_whole('d',True,expected_budget=budget['token']))
        self.assertEqual(r['status'],'failed')
        paid.assert_not_called()  # The single approved request is specific to the last batch.

if __name__=='__main__':unittest.main()

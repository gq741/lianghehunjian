"""Regression tests: local recovery must not replay paid requests."""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_whole_voice as fixture
from mixstudio import providers, media
from mixstudio.whole_voice import _atomic, _hash, _key


class VoiceRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionTests.tearDownClass.__func__)
    setUp = fixture.ProductionTests.setUp
    tearDown = fixture.ProductionTests.tearDown
    wait = fixture.ProductionTests.wait
    voice = fixture.ProductionTests.voice
    timing = fixture.ProductionTests.timing
    scenes = fixture.ProductionTests.scenes
    matching = fixture.ProductionTests.matching
    patches = fixture.ProductionTests.patches

    def test_definite_rejection_or_presubmit_cancel_does_not_fence_next_attempt(self):
        settings = self.studio.settings.public()
        for code in ('http_401', 'http_429', 'invalid_key', 'cancelled'):
            with self.subTest(code=code):
                error = providers.ProviderError('definite failure', code=code)
                with patch.object(providers, 'synthesize_voice', side_effect=error):
                    with self.assertRaises(providers.ProviderError):
                        self.studio._whole_audio(self.draft, settings, threading.Event())
                self.assertEqual(list((self.studio.root/'voice-cache').glob('*.pending.json')), [])

    def test_submitted_oversize_remains_unknown_and_is_not_replayed(self):
        error = providers.ProviderError('oversize', code='response_too_large', result_unknown=True)
        with patch.object(providers, 'synthesize_voice', side_effect=error) as paid:
            for _ in range(2):
                with self.assertRaises(providers.ProviderError) as caught:
                    self.studio._whole_audio(self.draft, self.studio.settings.public(), threading.Event())
                self.assertTrue(caught.exception.result_unknown)
            self.assertEqual(paid.call_count, 1)

    def test_preflight_names_the_missing_service(self):
        for name, label in (('deepseek','DeepSeek'), ('vision','GLM'), ('fish','Fish')):
            with self.subTest(name=name), patch.object(self.studio.settings, 'secret',
                    side_effect=lambda service: '' if service == name else 'offline-placeholder'):
                with self.assertRaisesRegex(ValueError, label):
                    self.studio._free_preflight(['a'], threading.Event(), lambda *a: None)

    def test_failed_asr_is_retained_but_does_not_permanently_block_local_retry(self):
        audio = self.root/'audio.wav'; fixture.wav(audio)
        def worker(command, **kwargs):
            path = Path(command[command.index('--output')+1])
            path.write_text(json.dumps([{'word':'完全不对应','start':0.,'end':1.}]), encoding='utf-8')
            return 0, ''
        with patch.object(providers, '_run_process', side_effect=worker) as run:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.studio._align_whole(self.draft, audio, _hash(audio), 5.,
                        self.studio.settings.public(), threading.Event())
            self.assertEqual(run.call_count, 2)

    def test_tiny_sentence_is_grouped_before_matching_timeline(self):
        draft = {**self.draft, 'segments': [
            {'segment_id':'s1','text':'甲。'}, {'segment_id':'s2','text':'乙。'},
            {'segment_id':'s3','text':'丙。'}], 'voiceover':'甲。乙。丙。',
            'whole_voice':{'duration':4., 'method':'local_asr_word_onsets_v1',
                'segment_seconds':[1.5,.03,2.47], 'sentence_groups':[['s1'],['s2','s3']]},
            'clips':self.scenes()['scenes'],
            'match_candidates':[{'segment_id':s,'clip_ids':['c']} for s in ('s1','s2','s3')]}
        result = self.studio._apply_timeline(draft, self.studio.settings.public())
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(draft['timeline_plan'], draft['whole_timeline_plan'])
        self.assertAlmostEqual(sum(s['voice_seconds'] for s in result['segments']), 4.)
        self.assertEqual(draft['unmatched'], [])

    def test_tiny_missing_candidate_is_not_filled_from_neighbor(self):
        draft = {**self.draft, 'whole_voice':{'duration':5., 'method':'test',
            'segment_seconds':[.03,4.97], 'sentence_groups':[['s1','s2']]},
            'clips':self.scenes()['scenes'], 'match_candidates':[
                {'segment_id':'s1','clip_ids':[]}, {'segment_id':'s2','clip_ids':['c']}]}
        result = self.studio._apply_timeline(draft, self.studio.settings.public())
        self.assertEqual(result['status'], 'shortage')
        self.assertIn('s1', draft['unmatched'])

    def test_local_realign_reuses_audio_and_stops_before_any_provider(self):
        with self.patches():
            first = self.wait(self.studio.produce_whole('d', True))
        self.assertEqual(first['status'], 'completed', first)
        saved = self.studio.store.draft('d')
        before = _hash(Path(saved['whole_audio']['path']))
        self.calls.clear()
        with patch.object(self.studio, '_align_whole', side_effect=self.timing), \
             patch.object(providers, 'synthesize_voice', side_effect=AssertionError('paid voice')), \
             patch.object(self.studio, '_match_operation', side_effect=AssertionError('paid match')):
            current = self.wait(self.studio.realign_whole('d'))
        self.assertEqual(current['status'], 'completed', current)
        self.assertEqual(current['kind'], 'alignment')
        self.assertEqual(before, _hash(Path(saved['whole_audio']['path'])))
        self.assertEqual(current['result']['committed'], True)

    def test_local_realign_without_cache_never_buys_audio(self):
        with patch.object(providers, 'synthesize_voice') as paid:
            with self.assertRaisesRegex(ValueError, '配音'):
                self.studio.realign_whole('d')
        paid.assert_not_called()

    def test_preflight_reuses_smokes_and_invalidates_only_changed_ocr_probe(self):
        probes = []
        def worker(command, **kwargs):
            probes.append(next(Path(str(x)).name for x in command if str(x).endswith('_worker.py')))
            return 0, ''
        with patch('mixstudio.whole_voice.importlib.util.find_spec', return_value=object()), \
             patch('mixstudio.subtitle_regions.probe_fingerprint', return_value=('ocr-v1',())) as identity, \
             patch.object(providers, '_run_process', side_effect=worker), \
             patch.object(self.studio, '_prepare_assets', return_value=[self.asset]), \
             patch.object(media, '_run') as ffmpeg:
            for _ in range(2):
                self.studio._free_preflight(['a'], threading.Event(), lambda *a: None)
            self.assertEqual(probes, ['voice_alignment_worker.py','subtitle_worker.py'])
            self.assertEqual(ffmpeg.call_count, 2)  # one decode and one encoder check
            identity.return_value = ('ocr-v2',())
            self.studio._free_preflight(['a'], threading.Event(), lambda *a: None)
            self.assertEqual(probes, ['voice_alignment_worker.py','subtitle_worker.py','subtitle_worker.py'])
            self.assertEqual(ffmpeg.call_count, 2)
            self.studio.settings.update({'preset':'fast'})
            self.studio._free_preflight(['a'], threading.Event(), lambda *a: None)
            self.assertEqual(ffmpeg.call_count, 3)

    def test_complete_invalid_provider_audio_still_keeps_unknown_fence(self):
        with patch.object(providers, '_post', return_value=(b'{"error":"bad"}', 'application/json')):
            with self.assertRaises(providers.ProviderError) as caught:
                self.studio._whole_audio(self.draft, self.studio.settings.public(), threading.Event())
        self.assertTrue(caught.exception.result_unknown)
        self.assertEqual(len(list((self.studio.root/'voice-cache').glob('*.pending.json'))), 1)

    def test_subframe_sentence_reaches_actual_ffmpeg_export(self):
        text = '甲。乙。丙。'
        segments = [{'segment_id':f's{i+1}', 'text':v} for i,v in enumerate(('甲。','乙。','丙。'))]
        self.studio.store.put_draft({**self.draft,'segments':segments,'voiceover':text})
        def paid(text,path,**kwargs):
            self.calls.append(('fish',text));fixture.wav(path,4.)
        def worker(command,**kwargs):
            output=Path(command[command.index('--output')+1])
            output.write_text(json.dumps([
                {'word':'甲','start':0.,'end':1.}, {'word':'乙','start':1.5,'end':1.52},
                {'word':'丙','start':1.53,'end':3.8}]),encoding='utf-8')
            return 0,''
        def matching(*args,**kwargs):
            self.calls.append(('deepseek',1))
            return {'candidates':[{'segment_id':s['segment_id'],'clip_ids':['c']} for s in segments]}
        with patch.object(self.studio, '_free_preflight'), \
             patch.object(providers,'synthesize_voice',side_effect=paid), \
             patch.object(providers,'_run_process',side_effect=worker), \
             patch('mixstudio.vision.analyze_asset',side_effect=self.scenes), \
             patch('mixstudio.script_engine.match_segments',side_effect=matching), \
             patch('mixstudio.subtitle_regions.locate',return_value=[]):
            job=self.wait(self.studio.produce_whole('d',True))
        self.assertEqual(job['status'],'completed',job)
        result=job['result'];self.assertTrue(Path(result['path']).is_file())
        self.assertAlmostEqual(result['duration'],4.,places=1)
        saved=self.studio.store.draft('d')
        self.assertEqual(''.join(c['text'] for c in saved['whole_voice']['captions']),text)
        self.assertEqual(saved['whole_voice']['sentence_groups'],[['s1'],['s2','s3']])
        self.assertEqual(saved['timeline_plan'],saved['whole_timeline_plan'])


if __name__ == '__main__':
    unittest.main()

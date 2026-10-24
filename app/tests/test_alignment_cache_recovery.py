"""Local ASR recovery never rebuying an utterance or weakening text checks."""
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

import test_voice_repair_invariants as fixture
from mixstudio import providers
from mixstudio.voice_timing import MIN_COVERAGE
from mixstudio.whole_voice import _atomic


class AlignmentCacheRecoveryTests(unittest.TestCase):
    setUp = fixture.VoiceRepairInvariantTests.setUp
    audio = fixture.VoiceRepairInvariantTests.audio
    legacy_asr = fixture.VoiceRepairInvariantTests.legacy_asr

    def align(self, audio, record, **kwargs):
        return self.studio._align_whole(self.draft, audio, record['sha256'], 4.,
            self.studio.settings.public(), threading.Event(), **kwargs)

    def worker(self, command, **kwargs):
        Path(command[command.index('--output')+1]).write_text(json.dumps(self.words),encoding='utf8')
        return 0, ''

    def bad_words(self):
        return [{'word':'完全无关的内容', 'start':0., 'end':3.8}]

    def test_low_coverage_legacy_cache_is_locally_recomputed_once(self):
        audio, record = self.audio()
        legacy = self.legacy_asr(record['sha256'], self.studio.settings.public())
        _atomic(legacy, {'words':self.bad_words()})
        before = audio.read_bytes()
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            timing = self.align(audio, record)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(timing['coverage'], 1.)
        self.assertEqual(audio.read_bytes(), before)

    def raw_cache(self, audio, record):
        with patch.object(providers, '_run_process', side_effect=self.worker):
            self.align(audio, record)
        paths = list((self.studio.root/'voice-cache').glob('alignment-*.json'))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_low_coverage_current_cache_is_not_a_permanent_failure(self):
        audio, record = self.audio()
        raw = self.raw_cache(audio, record)
        _atomic(raw, {'words':self.bad_words()})
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            timing = self.align(audio, record)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(timing['segment_seconds'], [2.,2.])
        self.assertEqual(json.loads(raw.read_text('utf8'))['words'], self.words)

    def test_fresh_unrelated_audio_still_stops_after_one_local_decode(self):
        audio, record = self.audio()
        self.words = self.bad_words()
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            with self.assertRaisesRegex(ValueError, '对应不足'):
                self.align(audio, record)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(MIN_COVERAGE, .55)
        self.assertTrue(audio.is_file())

    def test_failed_cached_recognition_can_be_retried_locally_without_loop(self):
        audio, record = self.audio()
        self.words = self.bad_words()
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            for expected in (1,2):
                with self.assertRaisesRegex(ValueError, '对应不足'):
                    self.align(audio, record)
                self.assertEqual(run.call_count, expected)

    def test_valid_current_cache_does_not_decode_or_rewrite(self):
        audio, record = self.audio()
        raw = self.raw_cache(audio, record)
        before = raw.read_bytes()
        with patch.object(providers, '_run_process', side_effect=AssertionError('valid cache')), \
             patch('mixstudio.whole_voice._atomic', side_effect=AssertionError('valid cache')):
            timing = self.align(audio, record)
        self.assertEqual(timing['coverage'], 1.)
        self.assertEqual(raw.read_bytes(), before)

    def test_good_legacy_cache_can_follow_bad_current_cache(self):
        audio, record = self.audio()
        raw = self.raw_cache(audio, record)
        _atomic(raw, {'words':self.bad_words()})
        self.legacy_asr(record['sha256'], self.studio.settings.public())
        with patch.object(providers, '_run_process', side_effect=AssertionError('valid legacy')):
            timing = self.align(audio, record)
        self.assertEqual(timing['coverage'], 1.)

    def test_refresh_ignores_valid_words_and_decodes_once(self):
        audio, record = self.audio()
        self.raw_cache(audio, record)
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            self.align(audio, record, refresh=True)
        self.assertEqual(run.call_count, 1)

    def test_bad_cached_missing_sentence_also_gets_a_local_repair(self):
        audio, record = self.audio()
        self.parts[:] = [{'segment_id':f's{i}', 'text':s} for i,s in enumerate(('甲乙丙。','丁戊己。','庚辛壬。'))]
        self.draft['voiceover'] = ''.join(s['text'] for s in self.parts)
        self.words = [{'word':s['text'],'start':float(i),'end':float(i+1)} for i,s in enumerate(self.parts)]
        raw = self.raw_cache(audio, record)
        _atomic(raw, {'words':[self.words[0],self.words[2]]})
        with patch.object(providers, '_run_process', side_effect=self.worker) as run:
            result = self.align(audio, record)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['coverage'],1.)

    def test_cancel_during_local_cache_repair_propagates_without_paid_call(self):
        from mixstudio.jobs import Cancelled
        audio, record = self.audio()
        legacy = self.legacy_asr(record['sha256'], self.studio.settings.public())
        _atomic(legacy, {'words':self.bad_words()})
        with patch.object(providers, '_run_process', side_effect=Cancelled('cancelled')) as run:
            with self.assertRaises(Cancelled):
                self.align(audio, record)
        self.assertEqual(run.call_count,1)

    def test_cache_write_failure_still_returns_valid_repaired_timing(self):
        audio, record = self.audio()
        legacy = self.legacy_asr(record['sha256'], self.studio.settings.public())
        _atomic(legacy, {'words':self.bad_words()})
        with patch.object(providers, '_run_process', side_effect=self.worker), \
             patch('mixstudio.whole_voice._atomic',side_effect=PermissionError('disk')):
            result = self.align(audio, record)
        self.assertEqual(result['coverage'],1.)
        self.assertTrue(any('缓存' in s for s in result['warnings']))


class WorkerParametersTests(unittest.TestCase):
    def test_whole_utterance_is_recognized_without_full_copy_prompt(self):
        from mixstudio import voice_alignment_worker as worker
        import tempfile
        captures=[]
        class Model:
            def __init__(self,*a,**k): pass
            def transcribe(self,*a,**k):
                captures.append(k)
                return [types.SimpleNamespace(words=[types.SimpleNamespace(word='测试',start=0.,end=1.)])],None
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'copy.txt').write_text('测试',encoding='utf8')
            argv=['worker','--source',str(root/'audio.wav'),'--model',str(root/'model'),
                '--text',str(root/'copy.txt'),'--output',str(root/'words.json')]
            with patch.dict(sys.modules,{'faster_whisper':types.SimpleNamespace(WhisperModel=Model)}), \
                 patch.object(sys,'argv',argv),patch.dict('os.environ',{},clear=False):
                self.assertEqual(worker.main(),0)
            self.assertNotIn('initial_prompt',captures[0])
            self.assertEqual(captures[0]['beam_size'],3)
            self.assertTrue(captures[0]['vad_filter'])
            self.assertTrue(captures[0]['word_timestamps'])
            self.assertFalse(captures[0]['condition_on_previous_text'])
            self.assertEqual(captures[0]['language'],'zh')


if __name__=='__main__': unittest.main()

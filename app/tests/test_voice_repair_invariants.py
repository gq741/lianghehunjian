"""Independent local-only invariants for cache reuse and alignment repair."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from mixstudio import providers
from mixstudio.service import Studio
from mixstudio.voice_timing import align_segments
from mixstudio.whole_voice import _atomic, _audio_key, _hash, _key


class VoiceRepairInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.studio = Studio(self.root/'runtime')
        self.addCleanup(self.studio.close)
        model = self.root/'model'; model.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (model/name).write_bytes(b'local fixture')
        self.studio.settings.update({'asr_model_path': str(model)})
        self.parts = [{'segment_id': 's1', 'text': '看看这款商品。'},
                      {'segment_id': 's2', 'text': '再看看细节。'}]
        self.words = [{'word': self.parts[0]['text'], 'start': .1, 'end': 1.5},
                      {'word': self.parts[1]['text'], 'start': 2., 'end': 3.8}]
        self.draft = {'id': 'd', 'workflow': 'script_first_v2', 'production_mode': 'whole_voice_v1',
                      'segments': self.parts, 'voiceover': ''.join(s['text'] for s in self.parts),
                      'target_seconds': 25, 'revision': 1, 'created_at': 1., 'asset_ids': [],
                      'clips': [], 'matches': [], 'status': 'draft', 'match_status': 'pending'}
        self.studio.store.put_draft(self.draft)
        self.guard = patch.object(providers, '_post', side_effect=AssertionError('paid request forbidden'))
        self.guard.start(); self.addCleanup(self.guard.stop)

    def wait(self, job):
        until = time.monotonic()+10
        while time.monotonic() < until:
            value = self.studio.store.job(job['id'])
            if value['status'] in {'completed', 'failed', 'cancelled'}:
                return value
            time.sleep(.01)
        self.fail('local alignment timed out')

    def audio(self):
        settings = self.studio.settings.public()
        key = _audio_key(self.draft, settings)
        path = self.studio.root/'voice-cache'/('whole-v1-'+key+'.wav')
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(b'\0\0'*32000)
        record = {'key': key, 'path': str(path), 'sha256': _hash(path), 'duration': 4.,
                  'voice_id': settings['voice_id']}
        self.draft['whole_audio'] = record
        self.studio.store.put_draft(self.draft)
        _atomic(path.with_suffix('.json'), {k: record[k] for k in ('sha256', 'duration', 'key')})
        return path, record

    def legacy_asr(self, signature, settings):
        model = Path(settings['asr_model_path'])
        version = [(n, (model/n).stat().st_size, (model/n).stat().st_mtime_ns)
                   for n in ('model.bin', 'config.json', 'tokenizer.json')]
        identity = _key(['word_onsets_v1', signature, self.parts, str(model), version])
        path = self.studio.root/'voice-cache'/('alignment-'+identity+'.json')
        _atomic(path, {'words': self.words})
        return path

    def test_valid_cached_asr_does_not_require_rewrite_permission(self):
        audio, record = self.audio()
        settings = self.studio.settings.public()
        self.legacy_asr(record['sha256'], settings)
        with patch('mixstudio.whole_voice._atomic', side_effect=PermissionError('readonly derived cache')), \
             patch.object(providers, '_run_process', side_effect=AssertionError('must reuse valid words')):
            result = self.studio._align_whole(self.draft, audio, record['sha256'], 4., settings, threading.Event())
        self.assertEqual(result['segment_seconds'], [2., 2.])

    def test_fresh_local_asr_can_finish_when_derived_cache_write_fails(self):
        audio, record = self.audio()
        def worker(command, **kwargs):
            Path(command[command.index('--output')+1]).write_text(json.dumps(self.words), encoding='utf-8')
            return 0, ''
        with patch.object(providers, '_run_process', side_effect=worker), \
             patch('mixstudio.whole_voice._atomic', side_effect=PermissionError('readonly derived cache')):
            result = self.studio._align_whole(self.draft, audio, record['sha256'], 4.,
                                             self.studio.settings.public(), threading.Event())
        self.assertEqual(result['segment_seconds'], [2., 2.])
        self.assertTrue(any('缓存' in warning for warning in result['warnings']))

    def test_changed_default_voice_does_not_block_local_realign_of_saved_audio(self):
        audio, record = self.audio()
        self.studio.settings.update({'voice_id': 'changed-default-voice'})
        def timing(*args, **kwargs):
            return align_segments(self.parts, self.words, 4.)
        with patch.object(self.studio, '_align_whole', side_effect=timing), \
             patch.object(providers, 'synthesize_voice', side_effect=AssertionError('local-only')), \
             patch.object(self.studio, '_match_operation', side_effect=AssertionError('local-only')):
            job = self.wait(self.studio.realign_whole('d'))
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(self.studio.store.draft('d')['whole_voice']['voice_id'], record['voice_id'])
        self.assertEqual(_hash(audio), record['sha256'])

    def test_local_realign_rejects_replaced_audio_without_paid_recovery(self):
        audio, _ = self.audio()
        audio.write_bytes(b'replaced fixture')
        with patch.object(self.studio, '_align_whole', side_effect=AssertionError('invalid audio must stop')), \
             patch.object(providers, 'synthesize_voice', side_effect=AssertionError('local-only')):
            job = self.wait(self.studio.realign_whole('d'))
        self.assertEqual(job['status'], 'failed', job)
        self.assertNotIn('whole_voice', self.studio.store.draft('d'))

    def test_local_realign_blocks_a_concurrent_produce_before_submission(self):
        self.audio()
        entered, release = threading.Event(), threading.Event()
        def timing(*args, **kwargs):
            entered.set(); release.wait(5)
            return align_segments(self.parts, self.words, 4.)
        with patch.object(self.studio, '_align_whole', side_effect=timing), \
             patch.object(providers, 'synthesize_voice', side_effect=AssertionError('local-only')):
            try:
                job = self.studio.realign_whole('d')
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(ValueError, '已有任务'):
                    self.studio.produce_whole('d', True)
            finally:
                release.set()
            self.assertEqual(self.wait(job)['status'], 'completed')

    def test_late_cancel_after_alignment_commit_keeps_completed_result(self):
        self.audio()
        event_holder = []
        def timing(*args, **kwargs):
            event_holder.append(args[5])
            return align_segments(self.parts, self.words, 4.)
        put = self.studio.store.put_draft
        def commit_then_cancel(draft):
            result = put(draft)
            if draft.get('whole_voice'):
                event_holder[0].set()
            return result
        with patch.object(self.studio, '_align_whole', side_effect=timing), \
             patch.object(self.studio.store, 'put_draft', side_effect=commit_then_cancel):
            job = self.wait(self.studio.realign_whole('d'))
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['result']['committed'], True)


if __name__ == '__main__':
    unittest.main()

"""Offline provider routing, historical cache, and local-repair integration.

Every runtime and byte fixture is temporary. Transport and video encoding are
guarded/mocked; these tests never contact either paid speech service.
"""
from contextlib import ExitStack, contextmanager
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import wave

from mixstudio import media, providers, speech
from mixstudio.http_app import create_app
from mixstudio.service import Studio, fingerprint
from mixstudio.voice_timing import align_segments
from mixstudio.whole_voice import _atomic, _audio_key, _hash, _pcm_duration


PARTS = [{'segment_id': 's1', 'text': '看看这款商品。'},
         {'segment_id': 's2', 'text': '再看看细节。'}]


def wav(path, seconds=4., sample=1, rate=8000):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(sample.to_bytes(2, 'little', signed=True) * round(rate * seconds))


class MinimaxVoiceRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='minimax-routing-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.studio = Studio(self.root / 'runtime')
        self.addCleanup(self.studio.close)
        model = self.root / 'model'
        model.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (model / name).write_bytes(b'offline model fixture')
        self.studio.settings.update({'output_dir': str(self.root / 'out'),
                                     'asr_model_path': str(model), 'transition': 'none'})
        source = self.root / 'product.mp4'
        source.write_bytes(b'mocked video source; encoding is not exercised here')
        self.asset = {'id': 'a', 'name': source.name, 'path': str(source),
                      'sha256': fingerprint(source), 'duration': 6.,
                      'width': 320, 'height': 240, 'imported_at': 1, 'media_info': {}}
        self.studio.store.put_asset(self.asset)
        self.draft = {'id': 'd', 'workflow': 'script_first_v2',
                      'production_mode': 'whole_voice_v1', 'segments': copy.deepcopy(PARTS),
                      'voiceover': ''.join(p['text'] for p in PARTS), 'target_seconds': 25,
                      'revision': 1, 'created_at': 1., 'asset_ids': ['a'], 'product_name': 'fixture',
                      'clips': [], 'matches': [], 'status': 'draft', 'match_status': 'pending'}
        self.studio.store.put_draft(self.draft)
        self.keys = {'fish': 'offline-fish-key', 'minimax': 'offline-minimax-key',
                     'deepseek': 'offline-deepseek-key', 'vision': 'offline-vision-key'}
        self.secret_reads = []
        self.secret_patch = patch.object(self.studio.settings, 'secret', side_effect=self.secret)
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        for name in ('_post', '_http_open'):
            guard = patch.object(providers, name, side_effect=AssertionError('unmocked network'))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []

    def secret(self, name):
        self.secret_reads.append(name)
        return self.keys.get(name, '')

    def settings(self, provider='fish', **changes):
        return {**self.studio.settings.public(), 'voice_provider': provider, **changes}

    def choose(self, provider, **changes):
        self.studio.settings.update({'voice_provider': provider, **changes})
        return self.studio.settings.public()

    def wait(self, job):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            value = self.studio.store.job(job['id'])
            if value['status'] in {'completed', 'failed', 'cancelled'}:
                return value
            time.sleep(.01)
        self.fail('offline routing task timed out')

    def voice(self, provider):
        def complete(text, path, **options):
            self.assertEqual(options['api_key'], self.keys[provider])
            self.calls.append((provider, text, options))
            wav(path, sample=1 if provider == 'fish' else 2)
            return {'path': str(path), 'duration': 4.}
        return complete

    @contextmanager
    def adapters(self):
        with patch.object(providers, 'synthesize_voice', side_effect=self.voice('fish')) as fish, \
             patch.object(providers, 'synthesize_minimax_voice', side_effect=self.voice('minimax')) as minimax:
            yield fish, minimax

    def audio(self, settings, retry=False, draft=None):
        return self.studio._whole_audio(draft or self.draft, settings, threading.Event(), retry)

    def marker(self, settings, attempt):
        key = _audio_key(self.draft, settings)
        marker = self.studio.root / 'voice-cache' / ('whole-v1-' + key + '.pending.json')
        _atomic(marker, {'attempt': attempt, 'state': 'unknown'})
        return marker

    def timing(self, draft, audio, signature, duration, settings, event, **kwargs):
        return align_segments(draft['segments'], [
            {'word': PARTS[0]['text'], 'start': .1, 'end': 1.5},
            {'word': PARTS[1]['text'], 'start': 2., 'end': 3.8}], duration)

    def test_fish_historical_whole_and_segment_key_goldens_stay_exact(self):
        settings = self.settings()
        self.assertEqual(_audio_key({'voiceover': 'fixture voice'}, settings),
                         'f2cc5c2515ede932f3424664266316dd4c3c993b09967e5e51bee1ddf8ab14ad')
        expected = '9397f74b0a7ee5f0c70884062c3f25a84dfc0e3d1a9f7a4658a4b103075e14aa'
        self.assertEqual(Studio._voice_key('fixture voice', settings['voice_id']), expected)
        self.assertEqual(Studio._voice_key('fixture voice', settings['voice_id'], settings=settings), expected)
        legacy = {k: v for k, v in settings.items() if k != 'voice_provider'}
        self.assertEqual(_audio_key(self.draft, legacy), _audio_key(self.draft, settings))

    def test_provider_model_and_voice_have_isolated_whole_and_segment_keys(self):
        variants = [self.settings(), self.settings('minimax'),
                    self.settings('minimax', minimax_model='speech-2.8-turbo'),
                    self.settings('minimax', minimax_voice_id='other-fixture-voice')]
        self.assertEqual(len({_audio_key(self.draft, s) for s in variants}), 4)
        self.assertEqual(len({Studio._voice_key('same text', s['voice_id'], settings=s)
                              for s in variants}), 4)
        original_whole = _audio_key(self.draft, variants[1])
        original_segment = Studio._voice_key('same text', variants[1]['voice_id'], settings=variants[1])
        with patch.object(providers, 'MINIMAX_URL', 'https://fixture.invalid/v1/t2a_v2'):
            self.assertNotEqual(_audio_key(self.draft, variants[1]),
                                original_whole)
            self.assertNotEqual(Studio._voice_key('same text', variants[1]['voice_id'], settings=variants[1]),
                                original_segment)

    def test_switching_back_reuses_original_fish_cache_and_only_selected_secret(self):
        variants = [self.settings(), self.settings('minimax'),
                    self.settings('minimax', minimax_model='speech-2.8-turbo'),
                    self.settings('minimax', minimax_voice_id='other-fixture-voice')]
        with self.adapters() as (fish, minimax):
            first = [self.audio(s) for s in variants]
            before = {str(row[0]): row[0].read_bytes() for row in first}
            for settings, previous in zip(variants, first):
                self.assertEqual(self.audio(settings), previous)
            self.assertEqual(fish.call_count, 1)
            self.assertEqual(minimax.call_count, 3)
        self.assertEqual(self.secret_reads, ['fish', 'minimax', 'minimax', 'minimax'])
        self.assertEqual(before, {str(row[0]): row[0].read_bytes() for row in first})
        self.assertTrue(all(call[2]['commit_complete'] for call in self.calls))
        self.assertEqual(self.calls[2][2]['model'], 'speech-2.8-turbo')
        self.assertEqual(self.calls[3][2]['voice_id'], 'other-fixture-voice')

    def test_legacy_fish_metadata_cache_is_reused_without_new_required_fields(self):
        settings = self.settings()
        key = _audio_key(self.draft, settings)
        path = self.studio.root / 'voice-cache' / ('whole-v1-' + key + '.wav')
        wav(path)
        _atomic(path.with_suffix('.json'), {'key': key, 'sha256': _hash(path), 'duration': 4.})
        with self.adapters() as (fish, minimax):
            self.assertEqual(self.audio(settings)[0], path)
            fish.assert_not_called()
            minimax.assert_not_called()
        self.assertEqual(self.secret_reads, [])

    def test_pending_markers_isolate_provider_model_and_voice_retry_attempts(self):
        variants = [self.settings(), self.settings('minimax'),
                    self.settings('minimax', minimax_model='speech-2.8-turbo'),
                    self.settings('minimax', minimax_voice_id='other-fixture-voice')]
        markers = [self.marker(s, 'attempt-' + str(i)) for i, s in enumerate(variants)]
        with self.adapters() as (fish, minimax):
            for settings in variants:
                with self.assertRaises(providers.ProviderError) as caught:
                    self.audio(settings)
                self.assertEqual(caught.exception.code, 'voice_result_unknown')
            with self.assertRaises(providers.ProviderError):
                self.audio(variants[1], 'attempt-0')
            fish.assert_not_called()
            minimax.assert_not_called()
            self.audio(variants[1], 'attempt-1')
            minimax.assert_called_once()
            fish.assert_not_called()
        self.assertFalse(markers[1].exists())
        self.assertTrue(all(markers[i].is_file() for i in (0, 2, 3)))

    def test_provider_failure_never_falls_back_and_unknown_request_is_not_replayed(self):
        for provider, adapter, other in (
                ('fish', 'synthesize_voice', 'synthesize_minimax_voice'),
                ('minimax', 'synthesize_minimax_voice', 'synthesize_voice')):
            with self.subTest(provider=provider), \
                 patch.object(providers, adapter, side_effect=providers.ProviderError(
                     'offline timeout', code='timeout', result_unknown=True)) as selected, \
                 patch.object(providers, other) as alternate:
                for _ in range(2):
                    with self.assertRaises(providers.ProviderError) as caught:
                        self.audio(self.settings(provider))
                    self.assertTrue(caught.exception.result_unknown)
                self.assertEqual(selected.call_count, 1)
                alternate.assert_not_called()
        self.assertEqual(self.secret_reads, ['fish', 'minimax'])

    @contextmanager
    def local_preflight(self):
        with ExitStack() as stack:
            stack.enter_context(patch('mixstudio.whole_voice.shutil.which', return_value=self.asset['path']))
            stack.enter_context(patch('mixstudio.whole_voice.importlib.util.find_spec', return_value=object()))
            stack.enter_context(patch('mixstudio.subtitle_regions.probe_fingerprint', return_value=('fixture', ())))
            stack.enter_context(patch.object(providers, '_run_process', return_value=(0, '')))
            stack.enter_context(patch.object(self.studio, '_unchanged'))
            stack.enter_context(patch.object(self.studio, '_prepare_assets', return_value=[self.asset]))
            stack.enter_context(patch.object(media, '_run'))
            yield

    def test_minimax_preflight_needs_no_fish_key_and_fish_needs_no_minimax_key(self):
        for provider, unused in (('minimax', 'fish'), ('fish', 'minimax')):
            with self.subTest(provider=provider):
                self.choose(provider)
                self.keys[unused] = ''
                self.keys[provider] = 'offline-' + provider + '-key'
                self.secret_reads.clear()
                with self.local_preflight():
                    result = self.studio._free_preflight(['a'], threading.Event(), lambda *a: None)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(self.secret_reads, ['deepseek', 'vision', provider])

    def test_preflight_snapshot_validates_captured_service_not_live_selection(self):
        snapshot = self.settings('minimax')
        self.choose('fish')
        self.keys['fish'] = ''
        with self.local_preflight():
            result = self.studio._free_preflight(['a'], threading.Event(), lambda *a: None,
                                                 settings_snapshot=snapshot)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(self.secret_reads, ['deepseek', 'vision', 'minimax'])

    def test_queued_whole_job_keeps_service_model_voice_snapshot(self):
        self.choose('minimax', minimax_model='speech-2.8-turbo', minimax_voice_id='queued-voice')
        captured = {}
        def submit(kind, payload, work, **kwargs):
            captured.update(kind=kind, payload=payload, work=work)
            return {'id': 'captured'}
        with patch.object(self.studio.jobs, 'submit', side_effect=submit):
            self.studio.produce_whole('d', True)
        self.choose('fish', minimax_model='speech-2.8-hd', minimax_voice_id='changed-voice')
        snapshots = []
        def preflight(*args, **kwargs):
            snapshots.append(speech.metadata(kwargs['settings_snapshot']))
        with self.adapters() as (fish, minimax), \
             patch.object(self.studio, '_free_preflight', side_effect=preflight), \
             patch.object(self.studio, '_align_whole', side_effect=ValueError('stop after saved audio')):
            with self.assertRaisesRegex(ValueError, 'stop after saved audio'):
                captured['work'](threading.Event(), lambda *args: None)
            fish.assert_not_called()
            minimax.assert_called_once()
        expected = {'provider': 'minimax', 'model': 'speech-2.8-turbo', 'voice_id': 'queued-voice'}
        self.assertEqual(captured['payload']['voice'], expected)
        self.assertEqual(snapshots, [expected])
        self.assertEqual(self.secret_reads, ['minimax'])
        record = self.studio.store.draft('d')['whole_audio']
        self.assertEqual({k: record[k] for k in expected}, expected)

    def test_minimax_alignment_failure_keeps_paid_audio_and_stops_before_matching(self):
        self.choose('minimax')
        with self.adapters() as (fish, minimax), \
             patch.object(self.studio, '_free_preflight'), \
             patch.object(self.studio, '_align_whole', side_effect=ValueError('offline alignment failure')), \
             patch.object(self.studio, '_match_operation') as matching:
            job = self.wait(self.studio.produce_whole('d', True))
            self.assertEqual(job['status'], 'failed', job)
            fish.assert_not_called()
            minimax.assert_called_once()
            matching.assert_not_called()
        record = self.studio.store.draft('d')['whole_audio']
        self.assertEqual(record['provider'], 'minimax')
        self.assertEqual(_hash(Path(record['path'])), record['sha256'])
        self.assertEqual(_pcm_duration(Path(record['path'])), 4.)

    def test_realign_uses_recorded_identity_after_defaults_change_including_legacy_fish(self):
        cases = [('fish', False), ('minimax', False), ('fish', True)]
        with self.adapters():
            for provider, legacy in cases:
                with self.subTest(provider=provider, legacy=legacy):
                    original = self.settings(provider, minimax_model='speech-2.8-turbo',
                                             minimax_voice_id='recorded-voice')
                    path, key, signature, duration = self.audio(original)
                    identity = speech.metadata(original)
                    record = {'path': str(path), 'key': key, 'sha256': signature, 'duration': duration,
                              **({'voice_id': original['voice_id']} if legacy else identity)}
                    self.studio.store.put_draft({**self.draft, 'whole_audio': record})
                    self.choose('fish' if provider == 'minimax' else 'minimax',
                                voice_id='changed-fish-voice', minimax_voice_id='changed-minimax-voice',
                                minimax_model='speech-2.8-hd')
                    self.secret_reads.clear()
                    with patch.object(providers, 'synthesize_voice') as fish, \
                         patch.object(providers, 'synthesize_minimax_voice') as minimax, \
                         patch.object(self.studio, '_align_whole', side_effect=self.timing), \
                         patch.object(self.studio, '_match_operation') as matching:
                        job = self.wait(self.studio.realign_whole('d', refresh=True))
                        self.assertEqual(job['status'], 'completed', job)
                        fish.assert_not_called()
                        minimax.assert_not_called()
                        matching.assert_not_called()
                    saved = self.studio.store.draft('d')['whole_voice']
                    self.assertEqual({k: saved[k] for k in identity}, identity)
                    self.assertEqual(_hash(path), signature)
                    self.assertEqual(self.secret_reads, [])

    def test_retry_both_entrypoints_reject_changed_provider_model_or_voice(self):
        original = self.settings('minimax')
        payload = {'draft_id': 'd', 'revision': 1, 'pipeline': 'whole_voice_v1',
                   'voice': speech.metadata(original)}
        self.studio.store.create_job('failed-voice', 'render', payload)
        self.studio.store.update_job('failed-voice', status='failed')
        changes = [('fish', {}), ('minimax', {'minimax_model': 'speech-2.8-turbo'}),
                   ('minimax', {'minimax_voice_id': 'new-voice'})]
        for provider, values in changes:
            self.choose('minimax', minimax_model='speech-2.8-hd', minimax_voice_id='male-qn-qingse')
            self.choose(provider, **values)
            for method in (self.studio.retry_whole_job, self.studio.retry):
                with self.subTest(provider=provider, values=values, method=method.__name__), \
                     patch.object(self.studio, 'produce_whole') as produce:
                    with self.assertRaisesRegex(ValueError, '已改变'):
                        method('failed-voice', True)
                    produce.assert_not_called()
        self.choose('minimax', minimax_model='speech-2.8-hd', minimax_voice_id='male-qn-qingse')
        with patch.object(self.studio, 'produce_whole', return_value={'id': 'retry'}) as produce:
            self.studio.retry_whole_job('failed-voice', True)
            produce.assert_called_once_with('d', True, retry_unknown=True, retry_payload=payload)

    def test_old_job_without_voice_identity_remains_fish_only(self):
        self.studio.store.create_job('old-job', 'render', {'draft_id': 'd', 'revision': 1,
                                                         'pipeline': 'whole_voice_v1'})
        self.studio.store.update_job('old-job', status='interrupted')
        self.choose('minimax')
        with patch.object(self.studio, 'produce_whole') as produce:
            with self.assertRaises(ValueError):
                self.studio.retry('old-job', True)
            produce.assert_not_called()
        self.choose('fish')
        with patch.object(self.studio, 'produce_whole', return_value={'id': 'retry'}) as produce:
            self.studio.retry('old-job', True)
            produce.assert_called_once_with('d', True, retry_unknown=True,
                                           retry_payload=self.studio.store.job('old-job')['payload'])

    def test_retry_selection_cannot_race_a_settings_switch_into_cross_provider_charge(self):
        legacy = self.legacy()
        methods = [(self.studio.retry_whole_job, 'd', 'whole_voice_v1'),
                   (self.studio.retry, 'd', 'whole_voice_v1'),
                   (self.studio.retry, legacy['id'], None)]
        for index, (method, draft_id, pipeline) in enumerate(methods):
            with self.subTest(method=method.__name__, pipeline=pipeline):
                selected = self.choose('fish')
                payload = {'draft_id': draft_id, 'revision': 1, 'voice': speech.metadata(selected)}
                if pipeline:
                    payload['pipeline'] = pipeline
                identifier = 'racing-job-' + str(index)
                self.studio.store.create_job(identifier, 'render', payload)
                self.studio.store.update_job(identifier, status='failed')
                original_public = self.studio.settings.public
                switched = []
                def snapshot_then_switch():
                    snapshot = original_public()
                    if not switched:
                        # Simulate another request committing after a read snapshot.
                        switched.append(True)
                        with self.studio.settings.lock:
                            self.studio.settings.values['voice_provider'] = 'minimax'
                    return snapshot
                with self.adapters() as (fish, minimax), self.legacy_media(), \
                     patch.object(self.studio.settings, 'public', side_effect=snapshot_then_switch), \
                     patch.object(self.studio, '_free_preflight'), \
                     patch.object(self.studio, '_align_whole', side_effect=ValueError('local stop')):
                    try:
                        result = method(identifier, True)
                    except ValueError:
                        pass  # Rejecting a changed selection before any paid request is valid.
                    else:
                        job = self.wait(result)
                        self.assertEqual(job['payload']['voice']['provider'], 'fish', job)
                    minimax.assert_not_called()

    def test_explicit_whole_retry_discovers_only_selected_pending_key(self):
        fish_marker = self.marker(self.settings(), 'old-fish-attempt')
        selected = self.choose('minimax')
        mini_marker = self.marker(selected, 'new-minimax-attempt')
        with self.adapters() as (fish, minimax), \
             patch.object(self.studio, '_free_preflight'), \
             patch.object(self.studio, '_align_whole', side_effect=ValueError('stop after retry audio')):
            first = self.wait(self.studio.produce_whole('d', True))
            self.assertEqual(first['status'], 'failed', first)
            self.assertTrue(first['result_unknown'])
            fish.assert_not_called()
            minimax.assert_not_called()
            retried = self.wait(self.studio.retry_whole_job(first['id'], True))
            self.assertEqual(retried['status'], 'failed', retried)
            self.assertIn('stop after retry audio', retried['error'])
            minimax.assert_called_once()
            fish.assert_not_called()
        self.assertTrue(fish_marker.is_file())
        self.assertFalse(mini_marker.exists())
        self.assertEqual(self.secret_reads, ['minimax'])

    def legacy(self, identifier='legacy'):
        draft = {'id': identifier, 'revision': 1, 'created_at': 1., 'title': 'legacy fixture',
                 'product_name': 'fixture', 'target_seconds': 25, 'asset_ids': ['a'],
                 'status': 'draft', 'segments': [{'clip_id': 'c', 'text': '旧分段配音测试。'}],
                 'clips': [{'clip_id': 'c', 'asset_id': 'a', 'source_path': self.asset['path'],
                            'start': 0., 'end': 6.}]}
        self.studio.store.put_draft(draft)
        return draft

    @contextmanager
    def legacy_media(self):
        def concat(paths, destination, event):
            wav(destination, sum(_pcm_duration(path) for path in paths), rate=48000)
        def render(clips, audio, output, settings, **kwargs):
            Path(output).write_bytes(b'mocked published video')
            return {'path': str(output), 'duration': _pcm_duration(audio), 'published': True}
        with patch.object(self.studio, '_unchanged'), \
             patch.object(self.studio, '_concat_voice', side_effect=concat), \
             patch.object(media, 'probe', side_effect=lambda path: {'duration': _pcm_duration(path)}), \
             patch.object(media, 'render_video', side_effect=render):
            yield

    def test_true_legacy_segment_route_uses_selected_provider_and_keeps_fish_cache(self):
        draft = self.legacy()
        mini = self.choose('minimax')
        self.keys['fish'] = ''
        with self.adapters() as (fish, minimax), self.legacy_media(), \
             patch.object(self.studio, 'produce_whole') as whole:
            first = self.wait(self.studio.render(draft['id'], True))
            self.assertEqual(first['status'], 'completed', first)
            self.assertEqual(first['payload']['voice'], speech.metadata(mini))
            self.assertEqual(self.secret_reads, ['minimax', 'minimax'])
            self.secret_reads.clear()
            second = self.wait(self.studio.render(draft['id'], True))
            self.assertEqual(second['status'], 'completed', second)
            minimax.assert_called_once()
            fish.assert_not_called()
            whole.assert_not_called()
            self.assertEqual(self.secret_reads, ['minimax'])
            self.keys['fish'] = 'offline-fish-key'
            fish_settings = self.choose('fish')
            third = self.wait(self.studio.render(draft['id'], True))
            self.assertEqual(third['status'], 'completed', third)
            fish.assert_called_once()
        cache = self.studio.root / 'voice-cache'
        fish_key = Studio._voice_key(draft['segments'][0]['text'], fish_settings['voice_id'])
        mini_key = Studio._voice_key(draft['segments'][0]['text'], mini['voice_id'], settings=mini)
        self.assertNotEqual(fish_key, mini_key)
        self.assertTrue((cache / (fish_key + '.wav')).is_file())
        self.assertTrue((cache / (mini_key + '.wav')).is_file())
        self.assertFalse(self.calls[0][2]['commit_complete'])
        self.assertNotIn('commit_complete', self.calls[-1][2])

    def test_legacy_segment_unknown_retry_stays_provider_bound_and_manual(self):
        draft = self.legacy()
        self.choose('minimax')
        error = providers.ProviderError('offline timeout', code='timeout', result_unknown=True)
        with patch.object(providers, 'synthesize_voice') as fish, \
             patch.object(providers, 'synthesize_minimax_voice', side_effect=error) as minimax, \
             self.legacy_media():
            first = self.wait(self.studio.render(draft['id'], True))
            self.assertEqual(first['status'], 'failed', first)
            second = self.wait(self.studio.render(draft['id'], True))
            self.assertEqual(second['status'], 'failed', second)
            self.assertEqual(minimax.call_count, 1)
            self.choose('fish')
            with self.assertRaises(ValueError):
                self.studio.retry(first['id'], True)
            self.choose('minimax')
            minimax.side_effect = self.voice('minimax')
            retried = self.wait(self.studio.retry(first['id'], True))
            self.assertEqual(retried['status'], 'completed', retried)
            self.assertEqual(minimax.call_count, 2)
            fish.assert_not_called()

    def test_legacy_v2_segmented_route_keeps_actual_timing_and_selected_provider(self):
        for provider, adapter, other in (
                ('fish', 'synthesize_voice', 'synthesize_minimax_voice'),
                ('minimax', 'synthesize_minimax_voice', 'synthesize_voice')):
            with self.subTest(provider=provider):
                selected = self.choose(provider)
                scenes = [{'clip_id': 'c' + str(i), 'asset_id': 'a',
                           'source_path': self.asset['path'], 'start': 3. * i, 'end': 3. * (i + 1),
                           'text': '商品外观', 'usable': True, 'tags': [], 'confidence': 1.}
                          for i in range(2)]
                candidates = [{'segment_id': part['segment_id'], 'clip_ids': [scene['clip_id']]}
                              for part, scene in zip(PARTS, scenes)]
                draft = {**self.draft, 'id': 'legacy-v2-' + provider, 'production_mode': 'legacy_v2',
                         'clips': scenes, 'match_candidates': candidates, 'matches': candidates,
                         'match_status': 'ready'}
                self.studio._apply_timeline(draft, selected)
                self.studio.store.put_draft(draft)
                def complete(text, output, **options):
                    self.assertEqual(options['api_key'], self.keys[provider])
                    wav(output, 2.)
                    return {'duration': 2.}
                with patch.object(providers, adapter, side_effect=complete) as voice, \
                     patch.object(providers, other) as alternate, self.legacy_media(), \
                     patch.object(self.studio, '_prepare_assets', return_value=[self.asset]), \
                     patch.object(media, '_window'), \
                     patch.object(self.studio, 'produce_whole') as whole:
                    job = self.wait(self.studio.render(draft['id'], True))
                    self.assertEqual(job['status'], 'completed', job)
                    self.assertEqual(voice.call_count, 2)
                    alternate.assert_not_called()
                    whole.assert_not_called()
                self.assertEqual(job['payload']['voice'], speech.metadata(selected))
                self.assertEqual(job['result']['voice_duration'], 4.)
                self.assertEqual(job['result']['timeline_plan']['phase'], 'actual')
                self.assertEqual(job['result']['voice_speed'], 1.)

    def test_historical_render_endpoint_still_redirects_whole_voice(self):
        self.choose('minimax')
        with patch.object(self.studio, 'produce_whole', return_value={'id': 'whole'}) as produce:
            self.assertEqual(self.studio.render('d', True), {'id': 'whole'})
            produce.assert_called_once_with('d', True, retry_unknown=False)

    def test_confirmed_voice_helper_requires_exact_non_secret_identity(self):
        selected = self.settings('minimax')
        identity = speech.metadata(selected)
        speech.require_confirmed_selection(None, selected)  # Internal historical default.
        speech.require_confirmed_selection(identity, selected)
        invalid = [[], '', True, {}, {**identity, 'label': 'MiniMax'},
                   {**identity, 'provider': 'fish'}, {**identity, 'model': 'speech-2.8-turbo'},
                   {**identity, 'voice_id': 'different-voice'}]
        for value in invalid:
            with self.subTest(expected=value), self.assertRaises(ValueError):
                speech.require_confirmed_selection(value, selected)
        self.assertEqual(self.secret_reads, [])

    def test_speech_helper_honors_independent_injected_provider_modules(self):
        fish_module = ModuleType('offline_fish_adapter_fixture')
        fish_module.synthesize_voice = Mock(side_effect=self.voice('fish'))
        mini_module = ModuleType('offline_minimax_adapter_fixture')
        mini_module.synthesize_minimax_voice = Mock(side_effect=self.voice('minimax'))
        with patch.object(providers, 'synthesize_voice', side_effect=AssertionError('global Fish adapter')) as fish, \
             patch.object(providers, 'synthesize_minimax_voice', side_effect=AssertionError('global MiniMax adapter')) as mini, \
             patch.object(providers, '_http_open', side_effect=AssertionError('global network')) as network:
            for provider, module in (('fish', fish_module), ('minimax', mini_module)):
                with self.subTest(provider=provider):
                    settings = self.settings(provider)
                    output = self.root / (provider + '-injected.wav')
                    result = speech.synthesize('injected provider fixture', output, settings=settings,
                        api_key=self.keys[provider], commit_complete=True, adapters=module)
                    self.assertEqual(result['path'], str(output))
                    self.assertEqual(_pcm_duration(output), 4.)
            fish.assert_not_called()
            mini.assert_not_called()
            network.assert_not_called()
        fish_module.synthesize_voice.assert_called_once()
        mini_module.synthesize_minimax_voice.assert_called_once()
        fish_options = fish_module.synthesize_voice.call_args.kwargs
        mini_options = mini_module.synthesize_minimax_voice.call_args.kwargs
        self.assertEqual(fish_options['voice_id'], self.settings()['voice_id'])
        self.assertNotIn('model', fish_options)
        self.assertEqual(mini_options['voice_id'], 'male-qn-qingse')
        self.assertEqual(mini_options['model'], 'speech-2.8-hd')
        self.assertTrue(fish_options['commit_complete'])
        self.assertTrue(mini_options['commit_complete'])

    def test_true_legacy_route_preserves_modules_injection_for_both_speech_services(self):
        modules = {}
        for provider, method in (('fish', 'synthesize_voice'), ('minimax', 'synthesize_minimax_voice')):
            module = ModuleType('offline_legacy_' + provider + '_fixture')
            setattr(module, method, Mock(side_effect=self.voice(provider)))
            modules[provider] = (module, method)
        with patch.object(providers, 'synthesize_voice', side_effect=AssertionError('global Fish adapter')) as fish, \
             patch.object(providers, 'synthesize_minimax_voice', side_effect=AssertionError('global MiniMax adapter')) as mini, \
             patch.object(providers, '_http_open', side_effect=AssertionError('global network')) as network:
            for provider, (module, method) in modules.items():
                with self.subTest(provider=provider):
                    self.choose(provider)
                    self.secret_reads.clear()
                    draft = self.legacy('injected-legacy-' + provider)
                    with self.legacy_media(), \
                         patch.object(self.studio, '_modules', return_value=(media, module)) as injected, \
                         patch.object(self.studio, 'produce_whole') as whole:
                        job = self.wait(self.studio.render(draft['id'], True))
                        self.assertEqual(job['status'], 'completed', job)
                        self.assertEqual(job['payload']['voice']['provider'], provider)
                        injected.assert_called_once()
                        whole.assert_not_called()
                    getattr(module, method).assert_called_once()
                    self.assertEqual(self.secret_reads, [provider, provider])
            fish.assert_not_called()
            mini.assert_not_called()
            network.assert_not_called()

    def test_confirmed_voice_mismatch_stops_whole_redirect_and_legacy_before_jobs_or_keys(self):
        current = self.choose('minimax')
        legacy = self.legacy()
        mismatches = [speech.metadata(self.settings('fish')),
                      {**speech.metadata(current), 'model': 'speech-2.8-turbo'},
                      {**speech.metadata(current), 'voice_id': 'different-voice'}]
        methods = [(self.studio.produce_whole, 'd'), (self.studio.render, 'd'),
                   (self.studio.render, legacy['id'])]
        with self.adapters() as (fish, minimax), patch.object(self.studio.jobs, 'submit') as submit:
            for expected in mismatches:
                for method, identifier in methods:
                    with self.subTest(method=method.__name__, identifier=identifier, expected=expected):
                        with self.assertRaisesRegex(ValueError, '已变化'):
                            method(identifier, True, expected_voice=expected)
            submit.assert_not_called()
            fish.assert_not_called()
            minimax.assert_not_called()
        self.assertEqual(self.secret_reads, [])

    def test_whole_redirect_preserves_confirmed_voice_identity(self):
        expected = speech.metadata(self.settings('minimax'))
        with patch.object(self.studio, 'produce_whole', return_value={'id': 'whole'}) as produce:
            self.studio.render('d', True, expected_voice=expected)
            produce.assert_called_once_with('d', True, retry_unknown=False, expected_voice=expected)

    def http_client(self):
        app = create_app(self.studio)
        return app.test_client(), {'X-Local-Token': app.config['LOCAL_TOKEN']}

    def test_http_both_paid_routes_forward_confirmed_voice_and_preserve_omitted_legacy_body(self):
        client, headers = self.http_client()
        identity = speech.metadata(self.settings('minimax'))
        for route, method in (('/api/v2/drafts/d/produce', 'produce_whole'),
                              ('/api/drafts/d/render', 'render')):
            with self.subTest(route=route):
                with patch.object(self.studio, method, return_value={'id': 'confirmed'}) as submit:
                    response = client.post(route, headers=headers,
                                           json={'allow_paid': True, 'voice': identity})
                    self.assertEqual(response.status_code, 200, response.get_json())
                    submit.assert_called_once_with('d', True, expected_voice=identity)
                with patch.object(self.studio, method, return_value={'id': 'legacy'}) as submit:
                    response = client.post(route, headers=headers, json={'allow_paid': True})
                    self.assertEqual(response.status_code, 200, response.get_json())
                    submit.assert_called_once_with('d', True)

    def test_http_changed_confirmation_and_explicit_invalid_voice_submit_zero_jobs(self):
        self.choose('minimax')
        client, headers = self.http_client()
        fish = speech.metadata(self.settings('fish'))
        for route in ('/api/v2/drafts/d/produce', '/api/drafts/d/render'):
            for expected in (fish, None, [], {}, True, 'minimax'):
                with self.subTest(route=route, expected=expected), \
                     patch.object(self.studio.jobs, 'submit', return_value={'id': 'unexpected'}) as submit, \
                     self.adapters() as (fish_adapter, minimax):
                    response = client.post(route, headers=headers,
                                           json={'allow_paid': True, 'voice': expected})
                    self.assertEqual(response.status_code, 400, response.get_json())
                    submit.assert_not_called()
                    fish_adapter.assert_not_called()
                    minimax.assert_not_called()
        self.assertEqual(self.secret_reads, [])


if __name__ == '__main__':
    unittest.main()

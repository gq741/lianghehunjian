"""Confirmed 2026-10-24 regressions. Provider calls are mocks, never network."""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mixstudio import diagnostics, media, providers, subtitle_regions, tasklog, vision, whole_voice
import test_providers as provider_fixture
import test_vision as vision_fixture
import test_voice_repair_subtitles as subtitle_fixture


class ProviderDiagnosticsRegressionTests(unittest.TestCase):
    def test_malformed_legacy_choice_is_sanitized(self):
        for choice in (None, [], 12):
            with self.subTest(choice=choice), patch.object(providers, '_post',
                    return_value=(json.dumps({'choices': [choice]}).encode(), 'application/json')):
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.rewrite_clips(provider_fixture.CLIPS, api_key='test-placeholder',
                        product_name='同款鞋', target_seconds=25, count=1,
                        model=providers.DEFAULT_DEEPSEEK_MODEL)
                self.assertEqual(caught.exception.code, 'invalid_response')
                self.assertNotIn('attribute', str(caught.exception))

    def test_actual_structured_error_codes_survive_allowlist_without_raw_fields(self):
        codes = ('asr_failed', 'download_failed', 'download_limit', 'download_tls',
            'download_transport', 'file_error', 'insufficient_material', 'invalid_download',
            'invalid_transcript', 'invalid_url', 'missing_dependency', 'missing_model',
            'output_exists', 'process_output', 'process_start')
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(diagnostics.normalize_diagnostic(
                    {'code': code, 'payload': 'SECRET', 'raw_response': 'SECRET'}), {'code': code})
        self.assertEqual(diagnostics.normalize_diagnostic({'code': 'sk-SECRET'}), {})

    def test_alignment_and_music_import_keep_their_task_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ('alignment', 'music_import'):
                tasklog.append_event(tmp, {'id': kind.replace('_', ''), 'kind': kind,
                                          'status': 'completed'})
            rows = [json.loads(line) for path in (Path(tmp)/'logs').glob('*.jsonl')
                    for line in path.read_text('utf8').splitlines()]
        self.assertEqual([row['kind'] for row in rows], ['alignment', 'music_import'])

    def test_semantic_cache_failure_uses_generic_cache_wording(self):
        error = vision.stage_error(providers.ProviderError('PRIVATE', code='invalid_cache'), 'DeepSeek画面匹配')
        self.assertIn('缓存校验失败', str(error))
        self.assertNotIn('画面缓存校验失败', str(error))
        self.assertNotIn('PRIVATE', str(error))

    def test_stop_timeout_does_not_replace_primary_cancellation(self):
        event = threading.Event()
        process = Mock(pid=42, returncode=None)
        process.poll.return_value = None
        def started(*args, **kwargs):
            event.set()
            return process
        with patch.object(providers.subprocess, 'Popen', side_effect=started), \
             patch.object(providers, '_stop_process', side_effect=subprocess.TimeoutExpired('private command', 10)):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._run_process(['fixture'], cancel=event)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertTrue(getattr(caught.exception, 'cleanup_failed', False))
        self.assertNotIn('private command', str(caught.exception))


class VisionCheckpointRegressionTests(unittest.TestCase):
    def setUp(self):
        vision_fixture.VisionPipelineTests.setUp(self)
        def discard_own_memory():
            if hasattr(vision, '_MEMORY_LOCK'):
                with vision._MEMORY_LOCK:
                    for key in list(vision._UNSAVED):
                        if key[0].startswith(str(self.root)):
                            vision._UNSAVED.pop(key)
        self.addCleanup(discard_own_memory)
    analyze = vision_fixture.VisionPipelineTests.analyze

    def test_tls_partial_mode_keeps_completed_cache_and_pauses_without_resubmit(self):
        self.probe.return_value['duration'] = 18
        def respond(*args, **kwargs):
            if self.post.call_count == 2:
                raise providers.ProviderError('PRIVATE TLS', code='tls', result_unknown=True)
            return vision_fixture.respond(*args, **kwargs)
        self.post.side_effect = respond
        result = self.analyze(allow_partial=True)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(result['analysis_summary']['completed'], 2)
        self.assertTrue(result['analysis_summary']['pause_new_requests'])
        self.assertEqual(result['analysis_summary']['stop_reason'], 'provider_unavailable')
        self.assertEqual(result['analysis_summary']['failed_batches'][0]['code'], 'tls')
        self.assertEqual(self.analyze(cache_only=True)['analysis_summary']['completed'], 2)
        self.assertEqual(self.post.call_count, 2)

    def test_unwritable_cache_stops_before_paid_request(self):
        with patch.object(vision.os, 'replace', side_effect=PermissionError('local disk blocked')):
            with self.assertRaises(providers.ProviderError) as caught:
                self.analyze()
        self.assertEqual(caught.exception.code, 'cache_write')
        self.assertFalse(caught.exception.result_unknown)
        self.post.assert_not_called()

    def test_paid_response_survives_disk_failure_then_promotes_without_second_call(self):
        fail = patch.object(vision.os, 'replace', side_effect=PermissionError('disk became full'))
        def response(*args, **kwargs):
            result = vision_fixture.respond(*args, **kwargs)
            fail.start()
            return result
        self.post.side_effect = response
        try:
            with self.assertRaises(providers.ProviderError) as caught:
                self.analyze()
            self.assertEqual(caught.exception.code, 'cache_write')
            self.assertFalse(caught.exception.result_unknown)
            self.assertIn('内存', str(caught.exception))
            self.assertIn('请勿关闭', str(vision.stage_error(caught.exception, '画面识别')))
            self.assertEqual(self.post.call_count, 1)
            cached = self.analyze(cache_only=True)
            self.assertEqual(cached['analysis_summary']['completed'], 2)
            self.assertEqual(self.post.call_count, 1)
            # Normal retry cannot start a new paid request while durability is broken.
            with self.assertRaises(providers.ProviderError):
                self.analyze()
            self.assertEqual(self.post.call_count, 1)
            fail.stop()
            self.post.side_effect = AssertionError('must reuse completed paid evidence')
            result = self.analyze()
            self.assertTrue(result['analysis_summary']['complete'])
            self.assertEqual(self.post.call_count, 1)
            self.assertEqual(len(list(self.cache.glob('vision-*.json'))), 1)
        finally:
            fail.stop()

    def test_memory_capacity_blocks_new_paid_calls_without_eviction(self):
        retained = ('another-cache', 'a' * 64)
        with patch.object(vision, 'MAX_UNSAVED_CACHES', 1), \
             patch.object(vision, '_RESERVED', set()), \
             patch.dict(vision._UNSAVED, {retained: 'original-paid-result'}, clear=True):
            with self.assertRaises(providers.ProviderError) as caught:
                self.analyze()
            self.assertEqual(caught.exception.code, 'cache_write')
            self.assertFalse(caught.exception.result_unknown)
            self.assertEqual(vision._UNSAVED, {retained: 'original-paid-result'})
        self.post.assert_not_called()
        self.frame.assert_not_called()

    def test_partial_disk_cache_merges_paid_memory_after_second_batch_save_failure(self):
        self.probe.return_value['duration'] = 12
        fail = patch.object(vision.os, 'replace', side_effect=PermissionError('disk became full'))
        def response(*args, **kwargs):
            result = vision_fixture.respond(*args, **kwargs)
            if self.post.call_count == 2:
                fail.start()
            return result
        self.post.side_effect = response
        try:
            with self.assertRaises(providers.ProviderError):
                self.analyze()
            self.assertEqual(self.post.call_count, 2)
            self.assertEqual(self.analyze(cache_only=True)['analysis_summary']['completed'], 4)
            fail.stop()
            self.post.side_effect = AssertionError('must not repay for either batch')
            self.assertEqual(self.analyze()['analysis_summary']['completed'], 4)
            self.assertEqual(self.post.call_count, 2)
        finally:
            fail.stop()

    def test_corrupt_disk_cannot_be_hidden_by_retained_paid_memory(self):
        fail = patch.object(vision.os, 'replace', side_effect=PermissionError('disk became full'))
        def response(*args, **kwargs):
            result = vision_fixture.respond(*args, **kwargs)
            fail.start()
            return result
        self.post.side_effect = response
        try:
            with self.assertRaises(providers.ProviderError):
                self.analyze()
            fail.stop()
            identity = vision._cache_identity(self.asset['sha256'], 6, self.options['endpoint'],
                self.options['model'], self.options['product_title'])
            vision._cache_path(self.cache, identity).write_text('{broken', encoding='utf8')
            with self.assertRaises(providers.ProviderError) as caught:
                self.analyze()
            self.assertEqual(caught.exception.code, 'invalid_cache')
            self.assertEqual(self.post.call_count, 1)
        finally:
            fail.stop()

    def test_duplicate_cache_fields_stop_without_another_paid_call(self):
        self.analyze()
        cache = next(self.cache.glob('vision-*.json'))
        original = cache.read_text('utf8')
        value = json.loads(original)
        variants = (
            original[:-1] + ', "records": []}',
            original[:-1] + ', "identity": ' + json.dumps(value['identity']) + '}',
            original.replace('"confidence": 0.9', '"confidence": 0.9, "confidence": 0.9', 1),
        )
        for raw in variants:
            with self.subTest(raw_kind=variants.index(raw)):
                cache.write_text(raw, encoding='utf8')
                with self.assertRaises(providers.ProviderError) as caught:
                    self.analyze()
                self.assertEqual(caught.exception.code, 'invalid_cache')
                self.assertEqual(self.post.call_count, 1)
        cache.write_text(original, encoding='utf8')

    def test_memory_result_is_not_used_for_another_product_context(self):
        fail = patch.object(vision.os, 'replace', side_effect=PermissionError('disk became full'))
        def response(*args, **kwargs):
            result = vision_fixture.respond(*args, **kwargs)
            fail.start()
            return result
        self.post.side_effect = response
        try:
            with self.assertRaises(providers.ProviderError):
                self.analyze()
            result = self.analyze(cache_only=True, product_title='不同商品')
            self.assertEqual(result['scenes'], [])
            self.assertEqual(self.post.call_count, 1)
        finally:
            fail.stop()


class SubtitleOptionalCacheRegressionTests(unittest.TestCase):
    setUp = subtitle_fixture.SubtitleCacheRepairTests.setUp
    frame = subtitle_fixture.SubtitleCacheRepairTests.frame
    worker = subtitle_fixture.SubtitleCacheRepairTests.worker
    locate = subtitle_fixture.SubtitleCacheRepairTests.locate

    def test_computed_band_survives_optional_cache_write_failure(self):
        with patch.object(whole_voice, '_atomic', side_effect=PermissionError('PRIVATE disk detail')):
            warnings = self.locate()
        self.assertIsNotNone(self.plan['windows'][0]['cleanup_band'])
        self.assertEqual(self.calls, 1)
        self.assertTrue(any('缓存' in warning and '保留' in warning for warning in warnings))
        self.assertNotIn('PRIVATE', ''.join(warnings))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'local FFmpeg required')
class PublishedRenderRegressionTests(unittest.TestCase):
    def test_prepublication_filesystem_failure_is_not_returned_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            warnings = []
            with self.assertRaisesRegex(OSError, 'fixture pre-publication'):
                with media._render_workspace(Path(tmp), warnings):
                    raise OSError('fixture pre-publication')
        self.assertEqual(warnings, [])

    def test_cleanup_failure_after_publish_returns_real_success(self):
        real_temp = media.tempfile.TemporaryDirectory
        class CleanupFailure:
            def __init__(self, *args, **kwargs):
                self.inner = real_temp(*args, **kwargs)
            def __enter__(self):
                return self.inner.__enter__()
            def __exit__(self, typ, error, traceback):
                self.inner.__exit__(typ, error, traceback)
                if typ is None:
                    raise PermissionError('PRIVATE post-publication cleanup')
        with real_temp() as tmp:
            root = Path(tmp)
            source, voice, output = root/'source.mp4', root/'voice.wav', root/'result.mp4'
            for args in (
                ['-f','lavfi','-i','testsrc2=s=160x160:r=30:d=1','-c:v','libx264','-preset','ultrafast','-threads','1',source],
                ['-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t','0.3',voice],
            ):
                subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n',*map(str,args)],
                               check=True, capture_output=True)
            with patch.object(media.tempfile, 'TemporaryDirectory', CleanupFailure):
                result = media.render_video([{'source_path':str(source), 'start':0., 'end':.8, 'text':'测试'}],
                    voice, output, {'width':160, 'height':160, 'fps':30, 'encoder':'cpu',
                    'preset':'fast','transition':'none','clean_mode':'none','subtitle_font_size':12})
            self.assertTrue(result['published'])
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(media.probe(output)['duration'], .3, delta=.04)
            self.assertTrue(any('临时' in value and '清理' in value for value in result['warnings']))
            self.assertNotIn('PRIVATE', ''.join(result['warnings']))


if __name__ == '__main__':
    unittest.main()

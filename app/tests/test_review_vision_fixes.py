"""R04/R06/R10 offline regressions: synthetic files and mocked providers only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from mixstudio import providers, script_engine as engine, vision


def wire(data):
    return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps(data, ensure_ascii=False)}}]}, ensure_ascii=False).encode(), 'application/json'


def observation(cid, confidence=.9):
    return {'clip_id': cid, 'description': '显示器正面', 'tags': ['外观'], 'ocr': '',
            'same_product': 'yes', 'flags': [], 'confidence': confidence, 'reason': '同型号正面'}


def candidates(body):
    return {'candidates': [{'segment_id': seg['segment_id'],
        'clip_ids': [s['clip_id'] for s in body['scenes'][:24]],
        'confidence': .9, 'reason': '同型号正面'} for seg in body['segments']], 'unmatched': []}


class VisualBoundsAndThresholdTests(unittest.TestCase):
    def test_visual_065_to_0699_reaches_semantic_match(self):
        planned = vision._plan('a'*64, 3)[0][0]
        for confidence in (.65, .67, .699):
            with self.subTest(confidence=confidence):
                scene = vision._scene({'id': 'a', 'path': 'unused.mp4'}, planned,
                    observation(planned['clip_id'], confidence), [{'mean': 100, 'black_ratio': 0}]*5)
                self.assertTrue(scene['usable'])
                with patch.object(engine, '_complete', side_effect=lambda _, data, **kw: candidates(data)) as api:
                    result = engine.match_segments([{'segment_id': 's1', 'text': '显示器正面。',
                        'visual_hint': '正面'}], [scene], api_key='fake-test', model='deepseek-chat', product_title='显示器')
                self.assertEqual(api.call_count, 1)
                self.assertEqual(result['unmatched'], [])

    def test_semantic_confidence_still_requires_070(self):
        segment = {'segment_id': 's1', 'required_seconds': 1}
        result = {'candidates': [{'segment_id': 's1', 'clip_ids': ['c1'],
                  'confidence': .67, 'reason': '不确定'}], 'unmatched': []}
        self.assertEqual(engine._validate_candidates(result, [segment], {'c1': {}}), [])

    def analyze(self, info):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.mp4'; source.write_bytes(b'synthetic-only')
            asset = {'id': 'a', 'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
            samples = []
            def frame(path, at, directory, index, cancel, start, end):
                samples.append((at, start, end))
                return b'\xff\xd8fixture\xff\xd9', {'mean': 100, 'black_ratio': 0}
            def post(url, payload, key, **kw):
                body = json.loads(payload['messages'][1]['content'][0]['text'])
                return wire({'observations': [observation(c['clip_id']) for c in body['clips']]})
            with patch.object(vision.media, 'probe', return_value=info), patch.object(vision, '_frame', side_effect=frame), patch.object(providers, '_post', side_effect=post) as api:
                result = vision.analyze_asset(asset, root/'cache', api_key='fake-test',
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='显示器')
                again = vision.analyze_asset(asset, root/'cache', api_key='fake-test',
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='显示器')
                self.assertEqual(result, again)
                self.assertEqual(api.call_count, 1)
            return result, samples

    def test_audio_tail_is_not_planned_as_video(self):
        result, samples = self.analyze({'duration': 7., 'width': 160, 'height': 90,
            'format_start_time': 0., 'video_start_time': 0., 'video_duration': 6., 'video_end_time': 6.})
        self.assertEqual([(s['start'],s['end']) for s in result['scenes']], [(0,3),(3,6)])
        self.assertTrue(all(at < 6 for at, _, _ in samples))

    def test_delayed_video_preserves_format_relative_seek_axis(self):
        result, samples = self.analyze({'duration': 8., 'width': 160, 'height': 90,
            'format_start_time': 10., 'video_start_time': 11., 'video_duration': 6., 'video_end_time': 17.})
        self.assertEqual([(s['start'],s['end']) for s in result['scenes']], [(1,4),(4,7)])
        self.assertTrue(all(1 < at < 7 for at, _, _ in samples))

    def test_nonzero_container_origin_does_not_shift_zero_local_video(self):
        result, _ = self.analyze({'duration': 6., 'width': 160, 'height': 90,
            'format_start_time': 10., 'video_start_time': 10., 'video_duration': 6., 'video_end_time': 16.})
        self.assertEqual([(s['start'],s['end']) for s in result['scenes']], [(0,3),(3,6)])

    def test_legacy_container_cache_keeps_only_identical_windows_and_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source.mp4'; source.write_bytes(b'synthetic-only')
            signature = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = {'id':'a','path':str(source),'sha256':signature}
            info = {'duration':7.,'width':160,'height':90,'format_start_time':0.,
                    'video_start_time':0.,'video_duration':6.,'video_end_time':6.}
            old_plan, _ = vision._plan(signature, 7.)
            old_id = vision._cache_identity(signature, 7., vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, '显示器')
            old_path = vision._cache_path(root/'cache', old_id)
            records = {s['clip_id']:{'planned':s,'observation':observation(s['clip_id']),
                        'quality':[{'mean':100,'black_ratio':0}]*len(s['sample_times'])} for s in old_plan[:2]}
            vision._save_cache(old_path, old_id, records); original = old_path.read_bytes()
            with patch.object(vision.media, 'probe', return_value=info), patch.object(vision, '_frame') as frame, patch.object(providers, '_post') as api, patch.object(vision, '_LOCKS', (threading.Lock(),)):
                result = vision.analyze_asset(asset, root/'cache', api_key='fake-test',
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='显示器')
            self.assertEqual(len(result['scenes']),2); api.assert_not_called(); frame.assert_not_called()
            self.assertEqual(old_path.read_bytes(), original)
            self.assertEqual(len(list((root/'cache').glob('vision-*.json'))), 2)

    def test_invalid_legacy_container_cache_does_not_bill_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.mp4'; source.write_bytes(b'synthetic-only')
            signature=hashlib.sha256(source.read_bytes()).hexdigest()
            info={'duration':7.,'width':160,'height':90,'format_start_time':0.,
                  'video_start_time':0.,'video_duration':6.,'video_end_time':6.}
            old_id=vision._cache_identity(signature,7.,vision.DEFAULT_ENDPOINT,vision.DEFAULT_MODEL,'显示器')
            old_path=vision._cache_path(root/'cache',old_id); old_path.write_text('{bad',encoding='utf8')
            with patch.object(vision.media,'probe',return_value=info), patch.object(providers,'_post') as api:
                with self.assertRaises(providers.ProviderError) as error:
                    vision.analyze_asset({'id':'a','path':str(source),'sha256':signature},root/'cache',
                        api_key='fake-test',endpoint=vision.DEFAULT_ENDPOINT,model=vision.DEFAULT_MODEL,product_title='显示器')
            self.assertEqual(error.exception.code,'invalid_cache');api.assert_not_called()
            self.assertEqual(old_path.read_text('utf8'),'{bad')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_audio_tail_analyzes_only_existing_video_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'video6-audio7.mp4'
            run=subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n',
                '-f','lavfi','-i','testsrc2=size=160x90:rate=10:duration=6',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=7',
                '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(source)],
                capture_output=True,timeout=40,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            self.assertEqual(run.returncode,0,run.stderr)
            original=source.read_bytes()
            asset={'id':'a','path':str(source),'sha256':hashlib.sha256(original).hexdigest()}
            def post(url,payload,key,**kw):
                body=json.loads(payload['messages'][1]['content'][0]['text'])
                return wire({'observations':[observation(c['clip_id']) for c in body['clips']]})
            options=dict(api_key='fake-test',endpoint=vision.DEFAULT_ENDPOINT,model=vision.DEFAULT_MODEL,product_title='显示器')
            with patch.object(providers,'_post',side_effect=post) as api:
                first=vision.analyze_asset(asset,root/'cache',**options)
                self.assertEqual(first,vision.analyze_asset(asset,root/'cache',**options))
            self.assertEqual([(s['start'],s['end']) for s in first['scenes']],[(0,3),(3,6)])
            self.assertEqual(api.call_count,1);self.assertEqual(source.read_bytes(),original)


class SemanticCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)/'semantic-cache'
        self.segments = [{'segment_id': 's1', 'text': '显示器正面。', 'visual_hint': '正面'}]
        self.scenes = [{'clip_id': f'c{i:03}', 'usable': True, 'confidence': .9,
            'start': i*3, 'end': i*3+3, 'text': '显示器正面', 'tags': ['外观'], 'ocr': '',
            'source_path': 'private-local-path.mp4', 'asset_id': 'asset'} for i in range(129)]
        self.context = {'draft_id': 'test-draft', 'revision': 1, 'sources': [{'id':'asset', 'sha256':'a'*64}]}
        self.calls = []

    def complete(self, system, data, **kwargs):
        self.calls.append([scene['clip_id'] for scene in data['scenes']])
        return candidates(data)

    def match(self, **updates):
        options = dict(api_key='fake-test', model='deepseek-chat', product_title='显示器',
                       cache_dir=self.cache, cache_context=self.context)
        options.update(updates)
        return engine.match_segments(self.segments, self.scenes, **options)

    def test_failed_later_batch_retry_reuses_confirmed_first_batch(self):
        def fail_second(system, data, **kwargs):
            result = self.complete(system, data, **kwargs)
            if len(self.calls) == 2:
                raise providers.ProviderError('mock timeout', code='network_timeout', result_unknown=True)
            return result
        with patch.object(engine, '_complete', side_effect=fail_second):
            with self.assertRaises(providers.ProviderError): self.match()
        self.assertEqual(len(list(self.cache.glob('semantic-*.json'))), 1)
        with patch.object(engine, '_complete', side_effect=self.complete):
            out = self.match()
            self.assertEqual(out, self.match())
        self.assertEqual([len(c) for c in self.calls], [128,1,1])

    def test_cancelled_later_batch_preserves_confirmed_first_batch(self):
        cancel = threading.Event()
        def progress(fraction, message):
            if fraction > 0: cancel.set()
        with patch.object(engine, '_complete', side_effect=self.complete):
            with self.assertRaises(providers.ProviderError): self.match(cancel=cancel, progress=progress)
            cancel.clear()
            self.match(cancel=cancel)
        self.assertEqual([len(c) for c in self.calls], [128,1])

    def test_corrupt_cache_never_silently_reposts(self):
        with patch.object(engine, '_complete', side_effect=self.complete): self.match()
        cache = next(self.cache.glob('semantic-*.json')); cache.write_text('{broken', encoding='utf8')
        with patch.object(engine, '_complete') as api, self.assertRaises(providers.ProviderError) as caught:
            self.match()
        self.assertEqual(caught.exception.code, 'invalid_cache'); api.assert_not_called()

    def test_cached_response_schema_is_revalidated(self):
        with patch.object(engine, '_complete', side_effect=self.complete): self.match()
        for path in self.cache.glob('semantic-*.json'):
            value = json.loads(path.read_text('utf8'))
            value['result']['candidates'][0]['clip_ids'] = ['unknown']
            path.write_text(json.dumps(value), encoding='utf8')
        with patch.object(engine, '_complete') as api, self.assertRaises(providers.ProviderError) as caught:
            self.match()
        self.assertEqual(caught.exception.code, 'invalid_cache'); api.assert_not_called()

    def test_invalid_provider_result_is_not_cached(self):
        with patch.object(engine, '_complete', return_value={'bad':'schema'}):
            with self.assertRaises(providers.ProviderError): self.match()
        self.assertFalse(list(self.cache.glob('semantic-*.json')))

    def test_atomic_cache_write_failure_does_not_retry_provider(self):
        with patch.object(engine, '_complete', side_effect=self.complete), patch('mixstudio.semantic_cache.os.replace', side_effect=OSError('fixture')):
            with self.assertRaises(providers.ProviderError) as error: self.match()
        self.assertEqual(error.exception.code, 'cache_write')
        self.assertFalse(error.exception.result_unknown)
        self.assertEqual(len(self.calls), 0)  # Fails before a paid request when neither checkpoint is writable.
        self.assertFalse(list(self.cache.glob('semantic-*.json')))
        self.assertFalse(list(self.cache.glob('semantic-write-*.tmp')))

    def test_cached_duplicate_json_keys_are_rejected_without_paid_call(self):
        with patch.object(engine, '_complete', side_effect=self.complete): self.match()
        for path in self.cache.glob('semantic-*.json'):
            raw = path.read_text('utf8')
            path.write_text(raw.replace('"version": 1', '"version": 1, "version": 1'), encoding='utf8')
        with patch.object(engine, '_complete') as api, self.assertRaises(providers.ProviderError) as error:
            self.match()
        self.assertEqual(error.exception.code, 'invalid_cache'); api.assert_not_called()

    def test_cache_identity_binds_context_text_model_prompt_and_full_visual_data(self):
        with patch.object(engine, '_complete', side_effect=self.complete):
            self.match(); self.match()
            self.assertEqual(len(self.calls), 2)
            changed = copy.deepcopy(self.context); changed['revision'] = 2
            self.match(cache_context=changed)
            self.match(model='deepseek-chat-other')
            self.segments[0]['text'] += '外观。'; self.match()
            self.scenes[0]['ocr'] = '只有 OCR 变化'; self.match()
            with patch.object(engine, 'MATCH_SYSTEM', engine.MATCH_SYSTEM+'\nchanged'): self.match()
        self.assertEqual(len(self.calls), 12)
        for path in self.cache.glob('semantic-*.json'):
            raw = path.read_text('utf8')
            self.assertNotIn('fake-test', raw); self.assertNotIn('private-local-path', raw)
            self.assertNotIn('test-draft', raw)

    def test_concurrent_identical_jobs_do_not_double_submit_successful_batch(self):
        def delayed(*args, **kwargs):
            time.sleep(.02)
            return self.complete(*args, **kwargs)
        with patch.object(engine, '_complete', side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.match(), range(2)))
        self.assertEqual(results[0], results[1]); self.assertEqual(len(self.calls), 2)

    def test_optional_cache_api_keeps_legacy_callers_working(self):
        with patch.object(engine, '_complete', side_effect=self.complete): self.match(cache_dir=None)
        self.assertFalse(self.cache.exists()); self.assertEqual(len(self.calls), 2)


if __name__ == '__main__': unittest.main()

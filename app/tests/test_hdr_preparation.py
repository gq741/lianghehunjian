"""Local synthetic PQ/HLG acceptance; every paid provider is a test double."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mixstudio import media, providers, script_engine, vision
from mixstudio.service import Studio, fingerprint
from mixstudio.timeline import reuse_stats

if importlib.util.find_spec('mixstudio.preprocess'):
    from mixstudio import preprocess
else:
    preprocess = None  # Restored OLD source must fail assertions, not fail to import tests.


def command(*args):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-n',
                             *map(str, args)], capture_output=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class HdrPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if preprocess is None:
            return
        cls.temp = tempfile.TemporaryDirectory(prefix='hdr-fixtures-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixtures = Path(cls.temp.name)
        for transfer in ['smpte2084', 'arib-std-b67']:
            command('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10:duration=2',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                '-vf', f'format=yuv420p10le,setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc:range=limited',
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-x264-params', f'colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc',
                '-c:a', 'aac', '-shortest', cls.fixtures / (transfer + '.mp4'))

    def setUp(self):
        self.assertIsNotNone(preprocess, 'HDR preparation integration is not present in this source version')
        self.temp = tempfile.TemporaryDirectory(prefix='hdr-case-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.fixtures / 'smpte2084.mp4'
        self.asset = {'id': 'hdr', 'name': self.source.name, 'path': str(self.source),
                      'sha256': fingerprint(self.source), 'duration': 2., 'width': 160, 'height': 90,
                      'imported_at': 1, 'clips': [], 'media_info': media.probe(self.source)}
        self.network = patch.object(providers, '_http_open', side_effect=AssertionError('Real APIs disabled'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def prepare(self, asset=None):
        return preprocess.prepare(asset or self.asset, self.root / 'cache')

    def studio(self):
        studio = Studio(self.root / 'runtime')
        self.addCleanup(studio.close)
        studio.settings.update({'output_dir': str(self.root / 'out'), 'width': 320, 'height': 480,
                                'fps': 25, 'clean_mode': 'none', 'transition': 'none',
                                'preset': 'fast', 'subtitle_font_size': 24})
        return studio

    def wait(self, studio, job):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = studio.store.job(job['id'])
            if state['status'] in {'completed', 'failed', 'cancelled'}:
                self.assertEqual(state['status'], 'completed', state)
                return state
            time.sleep(.02)
        self.fail('Local preparation/render timed out')

    def test_pq_and_hlg_convert_to_bt709_without_changing_originals_or_timing(self):
        for transfer in ['smpte2084', 'arib-std-b67']:
            source = self.fixtures / (transfer + '.mp4')
            asset = {**self.asset, 'path': str(source), 'sha256': fingerprint(source)}
            before = source.read_bytes()
            self.assertTrue(media.probe(source)['hdr'])
            record = self.prepare(asset)
            info = media.probe(Path(record['path']))
            self.assertEqual(record['mode'], 'sdr')
            self.assertFalse(info['hdr'])
            self.assertEqual(info['color_transfer'], 'bt709')
            self.assertEqual(info['color_primaries'], 'bt709')
            self.assertEqual(info['pix_fmt'], 'yuv420p')
            self.assertEqual((info['width'], info['height'], info['fps']), (160, 90, 10))
            self.assertAlmostEqual(info['video_duration'], 2., places=3)
            self.assertTrue(info['has_audio'])
            self.assertEqual(source.read_bytes(), before)

    def test_sdr_passthrough_creates_no_transcode_or_cache(self):
        source = self.root / 'ordinary.mp4'
        command('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10:d=1',
                '-c:v', 'libx264', '-preset', 'ultrafast', source)
        asset = {**self.asset, 'path': str(source), 'sha256': fingerprint(source)}
        with patch.object(media, '_run', wraps=media._run) as run:
            record = self.prepare(asset)
        self.assertEqual(record['mode'], 'original')
        self.assertFalse((self.root / 'cache').exists())
        self.assertFalse(any('libx264' in call.args[0] for call in run.call_args_list))

    def test_cache_reused_across_rename_without_encoding_again(self):
        first = self.prepare()
        renamed = self.root / 'renamed.mov'
        shutil.copyfile(self.source, renamed)
        with patch.object(media, '_run', wraps=media._run) as run:
            second = self.prepare({**self.asset, 'path': str(renamed)})
        self.assertEqual(first, second)
        self.assertFalse(any('libx264' in call.args[0] for call in run.call_args_list))

    def test_corrupt_cache_is_rebuilt_locally_not_used_or_charged(self):
        first = self.prepare()
        Path(first['path']).write_bytes(b'corrupt owned cache')
        second = self.prepare()
        self.assertEqual(preprocess.digest(Path(second['path'])), second['sha256'])
        self.assertFalse(media.probe(Path(second['path']))['hdr'])
        self.assertEqual(fingerprint(self.source), self.asset['sha256'])

    def test_cancel_before_or_during_encoding_publishes_no_partial_copy(self):
        event = threading.Event(); event.set()
        with self.assertRaises(media.MediaCancelled):
            preprocess.prepare(self.asset, self.root / 'cache', cancel=event)
        with patch.object(media, '_run', side_effect=media.MediaCancelled('cancel fixture')):
            with self.assertRaises(media.MediaCancelled):
                self.prepare()
        self.assertEqual(list((self.root / 'cache').glob('*.mp4')), [])
        self.assertEqual(list((self.root / 'cache').glob('*.json')), [])
        self.assertEqual(list((self.root / 'cache').glob('sdr-staging-*')), [])

    def test_source_change_and_unsupported_dolby_stop_before_transcode(self):
        with patch.object(media, '_run') as run, patch.object(media,'probe',
                return_value={**self.asset['media_info'],'dolby_vision_profile':5}):
            with self.assertRaises(media.MediaError):
                self.prepare({**self.asset, 'sha256': '0' * 64})
            with self.assertRaisesRegex(media.MediaError, 'Profile 5'):
                preprocess.prepare(self.asset, self.root / 'cache',
                    source_info={**self.asset['media_info'], 'dolby_vision_profile': 5})
        run.assert_not_called()

    def test_offset_and_variable_frame_timestamps_keep_original_clip_clock(self):
        source = self.root / 'offset-vfr.mp4'
        command('-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3',
            '-itsoffset', '0.4', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10:duration=2',
            '-map', '1:v:0', '-map', '0:a:0', '-vf',
            r'select=not(eq(mod(n\,3)\,1)),format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
            '-fps_mode', 'vfr', '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
            '-x264-params', 'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc', '-c:a', 'aac', source)
        asset = {**self.asset, 'path': str(source), 'sha256': fingerprint(source)}
        original = media.probe(source)
        self.assertGreater(original['video_start_time'] - original['format_start_time'], .3)
        record = self.prepare(asset)
        prepared = media.probe(Path(record['path']))
        self.assertAlmostEqual(original['video_duration'], prepared['video_duration'], delta=.11)
        self.assertAlmostEqual(original['video_start_time'] - original['format_start_time'],
            prepared['video_start_time'] - prepared['format_start_time'] + record['time_offset'], places=5)
        infos = {}
        checked = media._window({'source_path': str(source), 'source_sha256': asset['sha256'],
                                 'start': .6, 'end': 1.5, 'prepared_media': record}, infos)
        self.assertEqual(checked['start'], .6)
        self.assertEqual(checked['source_path'], source)
        self.assertEqual(infos[source]['render_path'], Path(record['path']))

    def test_authenticated_http_prepare_endpoint_and_preview_return_sdr(self):
        from mixstudio.http_app import create_app
        studio = self.studio()
        imported = studio._import_one(self.source, threading.Event())
        app = create_app(studio, token='test-local-token')
        with app.test_client() as client:
            self.assertEqual(client.post('/api/media/prepare', json={'asset_ids': [imported['id']]}).status_code, 403)
            client.get('/')
            response = client.post('/api/media/prepare', json={'asset_ids': [imported['id']]},
                                   headers={'X-Local-Token': 'test-local-token'})
            self.assertEqual(response.status_code, 200)
            job = response.get_json()['data']
            self.assertEqual(job['kind'], 'media_prepare')
            self.wait(studio, job)
            response = client.get('/media/assets/' + imported['id'])
            self.assertEqual(response.status_code, 200)
            saved = studio.store.asset(imported['id'])
            self.assertEqual(response.data, Path(saved['prepared_media']['path']).read_bytes())
            response.close()

    def test_concurrent_requests_encode_once(self):
        records, errors = [], []
        def worker():
            try: records.append(self.prepare())
            except Exception as error: errors.append(error)
        with patch.object(media, '_run', wraps=media._run) as run:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])
        self.assertEqual(sum('libx264' in call.args[0] for call in run.call_args_list), 1)

    def test_next_preparation_job_and_preview_use_copy_preserve_asset_identity(self):
        studio = self.studio()
        imported = studio._import_one(self.source, threading.Event())
        completed = self.wait(studio, studio.prepare_media([imported['id']]))
        self.assertEqual(completed['result']['sdr_copies'], 1)
        saved = studio.store.asset(imported['id'])
        self.assertEqual(saved['path'], imported['path'])
        self.assertEqual(saved['sha256'], imported['sha256'])
        self.assertEqual(studio.asset_media_path(imported['id']), Path(saved['prepared_media']['path']))
        self.assertIn('-sdr-v1', studio.asset_media_path(imported['id'], thumbnail=True).name)
        self.assertTrue(saved['selectable'])

    def test_visual_sampling_reads_sdr_but_returns_original_paths_and_clip_ids(self):
        from test_vision import respond
        record = self.prepare()
        asset = {**self.asset, 'prepared_media': record}
        with patch.object(providers, '_post', side_effect=respond) as post, \
             patch.object(vision, '_frame', wraps=vision._frame) as frame:
            first = vision.analyze_asset(asset, self.root / 'visual-cache', api_key='test-placeholder',
                endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='测试电视')
            second = vision.analyze_asset(asset, self.root / 'visual-cache', api_key='test-placeholder',
                endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='测试电视')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(first, second)
        self.assertTrue(all(c.args[0] == record['path'] for c in frame.call_args_list))
        self.assertEqual(first['scenes'][0]['source_path'], self.asset['path'])
        self.assertEqual(first['scenes'][0]['clip_id'], vision._plan(self.asset['sha256'], 2.)[0][0]['clip_id'])

    def test_unprepared_hdr_never_reaches_visual_api(self):
        with patch.object(providers, '_post') as post:
            with self.assertRaises(providers.ProviderError):
                vision.analyze_asset(self.asset, self.root / 'visual-cache', api_key='test-placeholder',
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='测试电视')
        post.assert_not_called()

    def test_prepared_copy_change_during_mock_response_never_commits_wrong_evidence(self):
        from test_vision import respond
        record = self.prepare()
        asset = {**self.asset, 'prepared_media': record}
        def changed(*args, **kwargs):
            result = respond(*args, **kwargs)
            with Path(record['path']).open('ab') as out:
                out.write(b'changed during mocked response')
            return result
        with patch.object(providers, '_post', side_effect=changed) as post:
            with self.assertRaises(providers.ProviderError) as caught:
                vision.analyze_asset(asset, self.root / 'visual-cache', api_key='test-placeholder',
                    endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='测试电视')
        self.assertEqual(caught.exception.code, 'source_changed')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(list((self.root / 'visual-cache').glob('vision-*.json')), [])

    def test_nonfinite_cached_clock_mapping_is_rejected(self):
        record = self.prepare()
        bad = {**record, 'time_offset': float('inf')}
        Path(record['path']).with_suffix('.json').write_text(json.dumps(bad), encoding='utf-8')
        with self.assertRaises(media.MediaError):
            preprocess.validated(bad, self.asset['sha256'])

    def test_copy_does_not_change_original_range_reuse_count(self):
        record = self.prepare()
        windows = [{'source_path': self.asset['path'], 'source_sha256': self.asset['sha256'],
                    'source_start': 0., 'source_end': 2., 'prepared_media': record} for _ in range(3)]
        self.assertEqual(reuse_stats(windows), {'repeat_count': 2, 'max_uses': 3})
        self.assertNotEqual(record['sha256'], self.asset['sha256'])

    def test_full_mocked_match_voice_and_real_render_export_sdr_then_reuse_audio(self):
        from test_vision import respond
        from test_media import tone
        studio = self.studio()
        asset = studio._import_one(self.source, threading.Event())
        draft = {'id': 'hdr-draft', 'workflow': 'script_first_v2', 'revision': 1, 'created_at': 1,
            'asset_ids': [asset['id']], 'target_seconds': 5, 'product_name': '测试电视',
            'voiceover': '展示测试电视。', 'segments': [{'segment_id': 's1', 'text': '展示测试电视。', 'visual_hint': '电视外观'}],
            'clips': [], 'matches': [], 'match_status': 'pending', 'status': 'draft'}
        studio.store.put_draft(draft)
        def semantic(segments, scenes, **kwargs):
            return {'matches': [{'segment_id': 's1', 'clip_ids': [scenes[0]['clip_id']], 'confidence': .9, 'reason': 'mock'}]}
        with patch.object(studio.settings, 'secret', return_value='test-placeholder'), \
             patch.object(providers, '_post', side_effect=respond), \
             patch.object(script_engine, 'match_segments', side_effect=semantic), \
             patch.object(providers, 'synthesize_voice', side_effect=lambda text, path, **kw: tone(path, 1.2)) as voice:
            self.wait(studio, studio.match_v2(draft['id'], True))
            first = self.wait(studio, studio.render(draft['id'], True))
            second = self.wait(studio, studio.render(draft['id'], True))
        self.assertEqual(voice.call_count, 1)
        output = Path(first['result']['path'])
        self.assertTrue(output.is_file())
        info = media.probe(output)
        self.assertFalse(info['hdr'])
        self.assertAlmostEqual(info['duration'], 1.2, delta=.08)
        self.assertNotEqual(first['result']['path'], second['result']['path'])
        self.assertEqual(first['result']['timeline_plan']['windows'][0]['source_sha256'], asset['sha256'])
        self.assertEqual(fingerprint(self.source), asset['sha256'])


if __name__ == '__main__':
    unittest.main()

"""Offline regressions for EOF/variable-frame-rate JPEG sampling."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mixstudio import media, providers, vision


class TailFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def fake_decode(self, args, **kwargs):
        jpg = next(Path(x) for x in args if str(x).endswith('.jpg'))
        gray = next(Path(x) for x in args if str(x).endswith('.gray'))
        jpg.write_bytes(b'\xff\xd8test\xff\xd9')
        gray.write_bytes(bytes([100]) * 4096)
        return ''

    def test_normal_frame_explicit_full_range_and_window_without_probe(self):
        with patch.object(media, '_run', side_effect=self.fake_decode) as run:
            value = vision._frame('source.mp4', 2.4, self.root, 'normal', None, 0, 3)
        args = run.call_args.args[0]
        self.assertIn('format=yuvj420p', args[args.index('-filter_complex') + 1])
        self.assertIn('trim=', args[args.index('-filter_complex') + 1])
        self.assertEqual(value[1]['mean'], 100)
        self.assertEqual(run.call_count, 1)

    def test_empty_frame_once_local_fallback_selects_integer_pts(self):
        def run(args, **kwargs):
            if '-copyts' in args:
                return self.fake_decode(args, **kwargs)
            return ''
        with patch.object(media, '_run', side_effect=run) as decode, patch.object(vision, '_fallback_timestamp', return_value=(3441664, 224.0)) as probe:
            data, _ = vision._frame('source.mp4', 224.085, self.root, 'tail', None, 224, 224.1)
        self.assertTrue(data.endswith(b'\xff\xd9'))
        self.assertEqual(decode.call_count, 2)
        probe.assert_called_once_with('source.mp4', 224.085, 224, 224.1, None)
        args = decode.call_args.args[0]
        self.assertIn('eq(pts\\,3441664)', args[args.index('-filter_complex') + 1])
        self.assertIn('-copyts', args)
        self.assertEqual(args[args.index('-ss') + 1], '224.000000000')

    def test_direct_five_argument_call_derives_three_second_window(self):
        with patch.object(media, '_run', side_effect=['', '']), patch.object(vision, '_fallback_timestamp', return_value=(1, 224)) as probe:
            with self.assertRaises(providers.ProviderError) as error:
                vision._frame('source.mp4', 224.085, self.root, 'tail', None)
        probe.assert_called_once_with('source.mp4', 224.085, 222.0, 225.0, None)
        self.assertEqual(error.exception.code, 'invalid_frame')

    def test_final_media_error_is_sanitized_and_unsubmitted(self):
        with patch.object(media, '_run', side_effect=media.MediaError('FFmpeg private raw error 0xabc')), patch.object(vision, '_fallback_timestamp', return_value=(1, 0)):
            with self.assertRaises(providers.ProviderError) as error:
                vision._frame('source.mp4', .5, self.root, 'failed', None, 0, 1)
        self.assertEqual(error.exception.code, 'invalid_frame')
        self.assertFalse(error.exception.result_unknown)
        self.assertNotIn('private', str(error.exception))
        self.assertNotIn('0xabc', str(error.exception))

    def test_cancellation_is_not_fallback(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(media, '_run') as run:
            with self.assertRaises(providers.ProviderError) as error:
                vision._frame('source.mp4', .5, self.root, 'cancel', cancel, 0, 1)
        self.assertEqual(error.exception.code, 'cancelled')
        run.assert_not_called()

    def test_media_cancelled_remains_cancelled(self):
        with patch.object(media, '_run', side_effect=media.MediaCancelled('cancelled')) as run:
            with self.assertRaises(providers.ProviderError) as error:
                vision._frame('source.mp4', .5, self.root, 'cancel', None, 0, 1)
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertEqual(run.call_count, 1)

    def test_corrupt_nonempty_output_is_not_accepted_or_retried(self):
        for jpg_data, gray_data in ((b"not-a-jpeg", bytes(4096)),
                                   (b"\xff\xd8test\xff\xd9", bytes(16))):
            with self.subTest(gray_size=len(gray_data)):
                def corrupt(args, **kwargs):
                    next(Path(x) for x in args if str(x).endswith('.jpg')).write_bytes(jpg_data)
                    next(Path(x) for x in args if str(x).endswith('.gray')).write_bytes(gray_data)
                with patch.object(media, '_run', side_effect=corrupt) as run, patch.object(vision, '_fallback_timestamp') as probe:
                    with self.assertRaises(providers.ProviderError) as error:
                        vision._frame('source.mp4', .5, self.root, 'bad-output', None, 0, 1)
                self.assertEqual(error.exception.code, 'invalid_frame')
                self.assertEqual(run.call_count, 1)
                probe.assert_not_called()

    def probe_fixture(self, frames, origin='0', time_base='1/1000'):
        return patch.object(vision, '_probe_json', side_effect=[{'format': {'start_time': origin}, 'streams': [{'time_base': time_base}]}, {'frames': [{'best_effort_timestamp': pts} for pts in frames]}])

    def test_vfr_uses_actual_previous_pts_not_fixed_fps(self):
        with self.probe_fixture([3990, 4100, 5500, 7990, 8010]):
            self.assertEqual(vision._fallback_timestamp('source', 7.9, 4, 8, None), (5500, 4.0))

    def test_no_cross_window_even_if_long_gop_probe_returns_earlier_frames(self):
        with self.probe_fixture([0, 1000, 3900, 8000, 9000]):
            with self.assertRaises(providers.ProviderError) as error:
                vision._fallback_timestamp('source', 7.9, 4, 8, None)
        self.assertEqual(error.exception.code, 'invalid_frame')

    def test_single_frame_in_window_can_be_reused(self):
        with self.probe_fixture([4500]):
            self.assertEqual(vision._fallback_timestamp('source', 4.1, 4, 8, None), (4500, 4.0))

    def test_nonzero_origin_keeps_absolute_pts_and_local_window_consistent(self):
        with self.probe_fixture([13800, 14000, 15250, 18000], origin='10.25'):
            self.assertEqual(vision._fallback_timestamp('source', 7.0, 4, 8, None), (15250, 14.25))

    def test_integer_pts_does_not_round_past_last_frame(self):
        with self.probe_fixture([3440640, 3441152, 3441664], time_base='1/15360'):
            self.assertEqual(vision._fallback_timestamp('source', 224.085, 224, 224.1, None), (3441664, 224.0))

    def test_probe_output_is_bounded(self):
        with patch.object(vision, 'MAX_FRAME_PROBE_BYTES', 128):
            with self.assertRaises(providers.ProviderError) as error:
                vision._probe_json([sys.executable, '-c', 'print("x"*4096)'], None)
        self.assertEqual(error.exception.code, 'invalid_frame')

    def test_probe_timeout_is_bounded(self):
        with patch.object(vision, 'FRAME_PROBE_TIMEOUT', .05):
            with self.assertRaises(providers.ProviderError) as error:
                vision._probe_json([sys.executable, '-c', 'import time; time.sleep(10)'], None)
        self.assertEqual(error.exception.code, 'invalid_frame')

    def test_invalid_probe_json_is_fixed_error(self):
        with self.assertRaises(providers.ProviderError) as error:
            vision._probe_json([sys.executable, '-c', 'print("private invalid data")'], None)
        self.assertEqual(error.exception.code, 'invalid_frame')
        self.assertNotIn('private', str(error.exception))

    def test_frame_failure_has_batch_context_and_no_paid_request(self):
        source = self.root / 'source.mp4'
        source.write_bytes(b'test')
        asset = {'id': 'fixture', 'path': str(source), 'sha256': hashlib.sha256(b'test').hexdigest()}
        with patch.object(media, 'probe', return_value={'duration': 8, 'width': 320, 'height': 240}), patch.object(vision, '_frame', side_effect=providers.ProviderError('local detail', code='invalid_frame')), patch.object(providers, '_post') as post:
            with self.assertRaises(providers.ProviderError) as error:
                vision.analyze_asset(asset, self.root / 'cache', api_key='test-fake-key', endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='test')
        post.assert_not_called()
        self.assertEqual(error.exception.vision_batch, (1, 2, 2))
        self.assertNotIn('local detail', str(error.exception))
        self.assertFalse(error.exception.result_unknown)

    def test_full_v2_cache_no_probe_frame_or_model(self):
        source = self.root / 'source.mp4'
        source.write_bytes(b'test')
        signature = hashlib.sha256(b'test').hexdigest()
        asset = {'id': 'fixture', 'path': str(source), 'sha256': signature}
        planned, _ = vision._plan(signature, 3)
        identity = vision._cache_identity(signature, 3, vision.DEFAULT_ENDPOINT, vision.DEFAULT_MODEL, 'test')
        records = {s['clip_id']: {'planned': s, 'quality': [{'mean':100,'black_ratio':0}]*3, 'observation': {'clip_id':s['clip_id'], 'description':'fixture', 'tags':[], 'ocr':'', 'same_product':'uncertain', 'flags':[], 'confidence':.5, 'reason':'fixture'}} for s in planned}
        cache = self.root / 'cache'
        vision._save_cache(vision._cache_path(cache, identity), identity, records)
        with patch.object(media, 'probe', return_value={'duration':3,'width':320,'height':240}), patch.object(vision, '_frame') as frame, patch.object(vision, '_probe_json') as probe, patch.object(providers, '_post') as post:
            result = vision.analyze_asset(asset, cache, api_key='test-fake-key', endpoint=vision.DEFAULT_ENDPOINT, model=vision.DEFAULT_MODEL, product_title='test')
        self.assertEqual(len(result['scenes']), 1)
        self.assertEqual(vision.ALGORITHM_VERSION, 2)
        frame.assert_not_called()
        probe.assert_not_called()
        post.assert_not_called()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
class RealTailFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def create(self, name, extra=()):
        source = self.root / name
        result = subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-f','lavfi','-i','testsrc2=s=160x90:r=30:d=0.1',*extra,'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(source)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))
        return source

    def test_real_short_tail_success_and_original_unchanged(self):
        source = self.create('tail.mp4')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with patch.object(providers, '_post') as post:
            jpeg, quality = vision._frame(str(source), .085, self.root, 'short', None, 0, .1)
        self.assertTrue(jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9'))
        self.assertTrue(0 <= quality['mean'] <= 255)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        post.assert_not_called()

    def test_real_nonzero_timestamp_tail(self):
        source = self.create('offset.mp4', ('-output_ts_offset','10'))
        jpeg, _ = vision._frame(str(source), .085, self.root, 'offset', None, 0, .1)
        self.assertTrue(jpeg.endswith(b'\xff\xd9'))

    def test_real_full_range_source_produces_complete_jpeg(self):
        source = self.create('full-range.mp4', ('-color_range','pc'))
        jpeg, _ = vision._frame(str(source), .015, self.root, 'full-range', None, 0, .1)
        self.assertTrue(jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"))

    def test_real_vfr_sparse_gap_never_uses_next_scene(self):
        source = self.root / 'sparse.mp4'
        result = subprocess.run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-f','lavfi','-i','color=c=black:s=160x90:r=10:d=0.2','-vf','drawbox=color=white:t=fill:enable=gte(n\\,1),setpts=N*4.5/TB','-fps_mode','vfr','-c:v','libx264','-threads','1',str(source)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))
        _, quality = vision._frame(str(source), 2.4, self.root, 'vfr', None, 0, 3)
        self.assertGreater(quality['black_ratio'], .99)


if __name__ == '__main__':
    unittest.main()

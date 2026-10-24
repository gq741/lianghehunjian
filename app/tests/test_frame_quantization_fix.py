"""Unequal-duration endpoint quantization regressions; no provider calls.

FIX_FRAME_SOURCE selects a source tree for red/green runs of this same test file.
The integration case generates its own source and PCM audio, runs the actual V2
planner and real FFmpeg renderer, and never accesses the user runtime.
"""
import hashlib
import math
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import unittest
import wave

SOURCE = Path(os.environ.get('FIX_FRAME_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))

from mixstudio import media
from mixstudio.timeline import plan_timeline, reuse_stats


LENGTHS = [1.728957, 1.728957, 2.15047633333333, 2.15047633333333,
           2.15047633333333, 2.336236, 2.313016, 2.313016, 1.755737,
           1.755737, 2.561315]
DURATION = 20.9444


class FrameQuantizationTests(unittest.TestCase):
    def assert_valid_frames(self, lengths, fade, fps, duration, cap=None):
        frames, actual_fade = media._frame_timeline(lengths, fade, fps, duration, cap)
        expected_total = math.ceil(duration * fps - 1e-8)
        if cap is not None:
            expected_total = min(expected_total, math.floor(cap * fps + 1e-8))
        overlap = round(actual_fade * fps)
        self.assertEqual(sum(frames) - overlap * (len(frames) - 1), expected_total)
        self.assertLessEqual(actual_fade, fade + 1e-10)
        for index, (count, length) in enumerate(zip(frames, lengths)):
            self.assertGreater(count, overlap, f'clip {index}')
            self.assertLessEqual(count / fps - length, 1 / fps + 1e-7,
                                 f'clip {index} extends by more than one frame')
        if cap is not None:
            self.assertLessEqual(expected_total / fps, cap + 1e-9)
        return frames

    def test_reported_eleven_unequal_windows_keep_tail_within_one_frame(self):
        self.assertAlmostEqual(sum(LENGTHS) - .2 * 10, DURATION, places=10)
        frames = self.assert_valid_frames(LENGTHS, .2, 30, DURATION)
        self.assertEqual(sum(frames) - 60, 629)
        self.assertEqual(frames[-1], 77)

    def test_minimal_unequal_hard_cut_does_not_charge_both_errors_to_tail(self):
        frames = self.assert_valid_frames([1.016, 1.03], 0, 30, 2.046)
        self.assertEqual(frames, [31, 31])

    def test_minimal_unequal_fade_keeps_same_total_frames(self):
        frames = self.assert_valid_frames([1.216, 1.03], .2, 30, 2.046)
        self.assertEqual(frames, [37, 31])

    def test_many_unequal_windows_fps_and_fractional_fades(self):
        rng = random.Random(20261022)
        for fps in (15, 24, 25, 30, 50, 60):
            for fade in (0, .07, .17, .2, .333):
                for count in (2, 3, 11, 32):
                    lengths = [2 * fade + .2 + rng.random() * .7 for _ in range(count)]
                    duration = sum(lengths) - fade * (count - 1)
                    self.assertLess(duration, 60)
                    with self.subTest(fps=fps, fade=fade, count=count):
                        self.assert_valid_frames(lengths, fade, fps, duration, 60)

    def test_hard_cap_and_fractional_upper_limit_remain_strict(self):
        for lengths, duration, cap in (([30.014, 29.986], 60., 60.),
                                       ([15.014, 15.001], 30.015, 30.015)):
            with self.subTest(cap=cap):
                self.assert_valid_frames(lengths, 0, 30, duration, cap)

    def test_genuine_source_shortage_is_still_rejected(self):
        with self.assertRaisesRegex(media.MediaError, '一帧补齐'):
            media._frame_timeline([1., 1.], 0, 30, 3.)

    def test_clip_shorter_than_output_frame_is_still_rejected(self):
        with self.assertRaises(media.MediaError):
            media._frame_timeline([1., .001], 0, 30, 1.001, 1.001)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class FrameQuantizationRealTests(unittest.TestCase):
    def test_actual_v2_plan_unequal_pcm_units_renders_without_extra_freeze(self):
        with tempfile.TemporaryDirectory(prefix='mix-frame-endpoints-') as folder:
            root = Path(folder)
            source, voice, output = root / 'source.mp4', root / 'voice.wav', root / 'result.mp4'
            media._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x160:rate=30:duration=33', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-threads', '1', source])
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            groups = (2, 3, 1, 2, 2, 1)
            scenes, segments, choices, voice_seconds = [], [], [], []
            cursor = 0
            for unit, count in enumerate(groups):
                sid = f's{unit}'
                segment_lengths = LENGTHS[cursor:cursor + count]
                seconds = (sum(segment_lengths) - .2 * (count - 1)
                           - .2 * ((unit > 0) + (unit < len(groups) - 1)) / 2)
                voice_seconds.append(seconds)
                segments.append(dict(segment_id=sid, text='合成测试画面'))
                ids = []
                for i in range(cursor, cursor + count):
                    cid = f'c{i}'
                    ids.append(cid)
                    scenes.append(dict(clip_id=cid, source_path=str(source),
                                       source_sha256=source_sha, start=3 * i, end=3 * i + 3,
                                       usable=True))
                choices.append(dict(segment_id=sid, clip_ids=ids))
                cursor += count
            plan = plan_timeline(segments, scenes, choices, target_seconds=25,
                                 transition_seconds=.2, voice_seconds=voice_seconds, phase='actual')
            self.assertEqual(plan['status'], 'ready')
            self.assertEqual(plan['speed'], 1.)
            self.assertEqual(reuse_stats(plan['windows'])['repeat_count'], 0)
            self.assertEqual(len(plan['windows']), 11)
            for expected, window in zip(LENGTHS, plan['windows']):
                self.assertAlmostEqual(window['end'] - window['start'], expected, places=9)
            with wave.open(str(voice), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(48000)
                stream.writeframes(b'\x01\x00' * round(DURATION * 48000))
            by_id = {scene['clip_id']: scene for scene in scenes}
            render_clips, captions = [], []
            current = 0.
            for row, segment, seconds in zip(choices, segments, voice_seconds):
                ids = row['clip_ids']
                render_clips.append({**by_id[ids[0]], 'text': segment['text'],
                                     'additional_clips': [by_id[cid] for cid in ids[1:]]})
                captions.append(dict(start=current, end=current + seconds, text=segment['text']))
                current += seconds
            result = media.render_video(render_clips, voice, output,
                dict(width=160, height=160, fps=30, encoder='cpu', preset='fast',
                     clean_mode='none', caption_plate=False, subtitle_font_size=12,
                     transition='fade', transition_seconds=.2, timeline_plan=plan,
                     duration_seconds=DURATION, subtitle_segments=captions,
                     min_output_seconds=20, max_output_seconds=30))
            info = media.probe(output)
            self.assertTrue(info['has_audio'])
            self.assertAlmostEqual(info['video_duration'], 629 / 30, delta=.001)
            self.assertLessEqual(abs(info['video_duration'] - info['audio_duration']), 1 / 30 + .001)
            self.assertEqual(sum(w['frame_count'] for w in result['timeline']) - 60, 629)
            for window in result['timeline']:
                self.assertLessEqual(window['duration'] - window['source_duration'], 1 / 30 + 1e-7)
                self.assertGreaterEqual(window['start'], window['source_start'])
                self.assertLessEqual(window['end'], window['source_end'] + 1e-7)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha)


if __name__ == '__main__':
    unittest.main(verbosity=2)

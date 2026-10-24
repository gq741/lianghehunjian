"""Caption cleanup placement: offline geometry plus real FFmpeg regression."""
import array
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

if os.environ.get('FIX_CLEANUP_SOURCE'):
    sys.path.insert(0, os.environ['FIX_CLEANUP_SOURCE'])
from mixstudio import media


class CaptionLayoutTests(unittest.TestCase):
    def test_cleanup_follows_each_shot_and_switches_at_fade_midpoint(self):
        timeline = [dict(output_start=0., output_end=2.),
                    dict(output_start=1.8, output_end=3.8)]
        layouts = media._caption_layouts(timeline, [(741, 814), (960, 1189)],
            [(437, 843), (0, 1280)], 3.8, 1280, 42, 12, 'delogo')
        self.assertEqual([(s['start'], s['end']) for s in layouts], [(0., 1.9), (1.9, 3.8)])
        self.assertEqual([s['center_y'] for s in layouts], [777, 1074])
        self.assertEqual([s['font_size'] for s in layouts], [42, 42])

    def test_cleanup_clamps_two_line_text_inside_actual_image_not_padding(self):
        layouts = media._caption_layouts([dict(output_start=0, output_end=2)],
            [(795, 839)], [(437, 842)], 2, 1280, 42, 12, 'delogo')
        item = layouts[0]
        self.assertLessEqual(item['center_y'] + item['font_size'] * 1.35 + 2, 842)
        self.assertGreaterEqual(item['center_y'] - item['font_size'] * 1.35 - 2, 437)
        self.assertLess(item['center_y'], 1084)

    def test_extreme_aspect_ratio_shrinks_caption_to_actual_image(self):
        item = media._caption_layouts([dict(output_start=0, output_end=2)],
            [(648, 652)], [(625, 655)], 2, 1280, 42, 12, 'blur')[0]
        self.assertLess(item['font_size'], 42)
        self.assertLessEqual(item['center_y'] + item['font_size'] * 1.35 + 2, 655)
        self.assertGreaterEqual(item['center_y'] - item['font_size'] * 1.35 - 2, 625)

    def test_no_cleanup_keeps_configured_bottom_margin(self):
        layouts = media._caption_layouts([dict(output_start=0, output_end=2)],
            [(741, 814)], [(437, 843)], 2, 1280, 42, 12, 'none')
        self.assertEqual(layouts, [dict(start=0., end=2, center_y=1084, font_size=42)])

    def test_no_cleanup_preserves_original_clamped_margin_at_extreme_font_sizes(self):
        for height, font_size, margin, expected in [(160, 120, 40, 69),
                                                   (240, 96, 40, 88),
                                                   (480, 20, 12, 402)]:
            with self.subTest(height=height, font_size=font_size, margin=margin):
                item = media._caption_layouts([dict(output_start=0, output_end=2)],
                    [(0, height)], [(0, height)], 2, height, font_size, margin, 'none')[0]
                self.assertEqual(item['center_y'], expected)
                self.assertGreaterEqual(item['center_y'], 0)
                self.assertLessEqual(item['center_y'], height)

    def test_ass_shot_split_does_not_restart_caption_pagination(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'captions.ass'
            # width=160/font=20 => 7 characters per line, 14 per page.
            text = 'abcdefghijklmnOPQRSTUVWXYZ12'
            media._write_captions(path, [dict(start=0, end=4, text=text)],
                160, 480, 20, 400, layouts=[
                    dict(start=0, end=1, center_y=300, font_size=20),
                    dict(start=1, end=4, center_y=420, font_size=20)])
            content = path.read_text('utf-8')
        events = [s for s in content.splitlines() if s.startswith('Dialogue:')]
        self.assertEqual(len(events), 3)
        self.assertIn('0:00:00.00,0:00:01.00', events[0])
        self.assertIn('0:00:01.00,0:00:02.00', events[1])
        self.assertIn('0:00:02.00,0:00:04.00', events[2])
        self.assertIn(r'abcdefg\Nhijklmn', events[0])
        self.assertIn(r'abcdefg\Nhijklmn', events[1])
        self.assertIn(r'OPQRSTU\NVWXYZ12', events[2])
        self.assertIn(r'\pos(80,300)', events[0])
        self.assertIn(r'\pos(80,420)', events[1])
        self.assertIn(',1,2,0,5,20,20,20,1', content)  # BorderStyle 1: outline, not box.


def run(*args):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', *map(str, args)],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class CaptionRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='jdmix-cleanup-caption-')
        cls.root = Path(cls.temp.name)
        cls.sources = []
        for index, size in enumerate(('320x180', '180x320')):
            path = cls.root / f'source{index}.mp4'
            run('-n', '-f', 'lavfi', '-i', f'color=c=green:s={size}:r=30:d=2',
                '-vf', 'drawbox=x=30:y=ih*0.94:w=30:h=ih*0.035:color=white:t=fill',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', path)
            cls.sources.append(path)
        cls.voice = cls.root / 'voice.wav'
        samples = array.array('h', (int(4000*math.sin(2*math.pi*440*i/24000)) for i in range(86400)))
        with wave.open(str(cls.voice), 'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000); f.writeframes(samples.tobytes())
        cls.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [*cls.sources, cls.voice]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def render(self, name, plate=None):
        ass = []; graphs = []
        original_captions, original_run = media._write_captions, media._run
        def capture(path, *args, **kwargs):
            original_captions(path, *args, **kwargs); ass.append(path.read_text('utf-8'))
        def capture_run(args, **kwargs):
            if '-filter_complex' in args:
                graphs.append(str(args[args.index('-filter_complex')+1]))
            return original_run(args, **kwargs)
        settings = dict(width=320, height=480, fps=30, preset='fast', encoder='cpu',
            transition='fade', transition_seconds=.2, clean_mode='delogo', clean_top_pct=88,
            clean_height_pct=11, subtitle_font_size=20, subtitle_segments=[
                dict(start=0, end=1.8, text='新的字幕甲'), dict(start=1.8, end=3.6, text='新的字幕乙')])
        if plate is not None:
            settings['caption_plate'] = plate
        with patch.object(media, '_write_captions', side_effect=capture), patch.object(media, '_run', side_effect=capture_run):
            result = media.render_video([dict(source_path=str(p), start=0, end=2, text='新的字幕')
                for p in self.sources], self.voice, self.root / name, settings)
        for p, expected in self.hashes.items():
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), expected)
        return result, ass[0], graphs

    def test_real_cleaned_subtitles_follow_images_without_plate(self):
        result, ass, graphs = self.render('no-plate.mp4', False)
        self.assertNotIn('drawbox=', '\n'.join(graphs))
        self.assertEqual(len([s for s in graphs if 'delogo=' in s]), 2)
        positions = [int(y) for y in re.findall(r'\\pos\(160,(\d+)\)', ass)]
        self.assertTrue(positions)
        self.assertLess(positions[0], 330)  # landscape image ends at 330, not padding at 402.
        self.assertGreater(positions[-1], 420)
        self.assertTrue(media.probe(Path(result['path']))['has_audio'])
        raw = run('-ss', '0.5', '-i', result['path'], '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1')
        # Left edge inside clean band must retain image, not an opaque full-width plate.
        green = raw[(315*320+15)*3:(315*320+15)*3+3]
        self.assertGreater(green[1], 70)

    def test_default_has_no_plate_but_explicit_true_is_compatible(self):
        _, _, defaults = self.render('default.mp4')
        self.assertNotIn('drawbox=', '\n'.join(defaults))
        _, _, explicit = self.render('legacy-plate.mp4', True)
        self.assertIn('drawbox=', '\n'.join(explicit))


if __name__ == '__main__':
    unittest.main(verbosity=2)

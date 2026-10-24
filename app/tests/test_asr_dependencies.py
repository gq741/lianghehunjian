"""Real audio-decode compatibility, without model downloads or paid requests."""
from importlib.metadata import version
from pathlib import Path
import math
import struct
import tempfile
import unittest
import wave


class ASRDependencyTests(unittest.TestCase):
    def test_real_whisper_audio_decode(self):
        from faster_whisper.audio import decode_audio
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'sample.wav'
            with wave.open(str(source), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(i / 16000 * 440 * math.tau)))
                                           for i in range(16000)))
            decoded = decode_audio(str(source))
        self.assertEqual(decoded.shape, (16000,))
        self.assertEqual(str(decoded.dtype), 'float32')
        self.assertGreater(float(abs(decoded).max()), .1)

    def test_pinned_decoder_matches_current_whisper_contract(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('av>=18.1,<19', (root / 'requirements.txt').read_text('utf-8'))
        self.assertIn('av==18.1.0', (root / 'requirements-lock.txt').read_text('utf-8'))
        if version('faster-whisper') == '1.2.1':
            self.assertLess(int(version('av').split('.')[0]), 19)


if __name__ == '__main__':
    unittest.main()

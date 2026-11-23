"""Paid-voice cache recovery with real PCM files and a local fake provider."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from mixstudio import providers
from mixstudio.whole_voice import WholeVoiceActions, _audio_key, _atomic


class CacheRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.studio = WholeVoiceActions()
        self.studio.root = Path(tmp.name)
        (self.studio.root / 'work').mkdir()
        (self.studio.root / 'voice-cache').mkdir()
        self.studio.voice_slots = [threading.Semaphore(1) for _ in range(256)]
        self.studio.settings = SimpleNamespace(secret=lambda _: 'fixture-only')
        self.settings = {'voice_provider':'fish', 'voice_id':'local-fixture-voice'}
        self.draft = {'voiceover':'这是同一条测试口播。'}
        self.calls = []
        def synth(*args, **kwargs):
            self.calls.append(kwargs['settings']['voice_provider'])
            with wave.open(str(args[1]), 'wb') as o:
                o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000)
                o.writeframes(bytes([len(self.calls),0])*16000)
        p = patch('mixstudio.whole_voice.synthesize_speech', side_effect=synth)
        p.start(); self.addCleanup(p.stop)

    def audio(self, retry=False):
        return self.studio._whole_audio(self.draft,self.settings,threading.Event(),retry)

    def test_lost_wav_with_metadata_needs_distinct_retry_before_rebuy(self):
        path,_,_,_ = self.audio()
        path.unlink()
        with self.assertRaisesRegex(providers.ProviderError,'配音文件缺失') as first:
            self.audio()
        self.assertFalse(first.exception.result_unknown)
        marker = path.with_suffix('.pending.json')
        token = json.loads(marker.read_text('utf8'))['attempt']
        for retry in (False, 'old-attempt'):
            with self.assertRaisesRegex(providers.ProviderError,'配音文件缺失'):
                self.audio(retry)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(path.with_suffix('.json').is_file())
        new = self.audio(token)
        self.assertEqual(len(self.calls),2)
        self.assertFalse(marker.exists())
        self.assertEqual(self.audio(token),new)
        self.assertEqual(len(self.calls),2)

    def test_draft_receipt_alone_protects_missing_audio_and_accepts_restored_bytes(self):
        path,key,signature,duration = self.audio()
        original = path.read_bytes()
        self.draft['whole_audio'] = dict(key=key,sha256=signature,duration=duration,path=str(path))
        path.unlink();path.with_suffix('.json').unlink()
        with self.assertRaisesRegex(providers.ProviderError,'配音文件缺失'):
            self.audio()
        path.write_bytes(original)
        self.assertEqual(self.audio(),(path,key,signature,duration))
        self.assertEqual(len(self.calls),1)
        self.assertTrue(path.with_suffix('.json').is_file())

    def test_authorized_replacement_survives_failure_writing_receipt(self):
        path,_,_,_ = self.audio()
        path.unlink()
        with self.assertRaises(providers.ProviderError):
            self.audio()
        marker=path.with_suffix('.pending.json')
        token=json.loads(marker.read_text('utf8'))['attempt']
        def fail_receipt(path_, value):
            if path_ == path.with_suffix('.json'):
                raise OSError('fixture: receipt write interrupted')
            _atomic(path_,value)
        with patch('mixstudio.whole_voice._atomic', side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError,'interrupted'):
                self.audio(token)
        self.assertTrue(path.exists())
        self.assertNotEqual(json.loads(path.with_suffix('.json').read_text('utf8'))['sha256'],
                            hashlib.sha256(path.read_bytes()).hexdigest())
        self.audio()
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.calls),2)

    def test_missing_receipt_does_not_affect_other_provider(self):
        path,key,digest,seconds=self.audio()
        self.draft['whole_audio']=dict(key=key,sha256=digest,duration=seconds,path=str(path))
        path.unlink()
        self.settings['voice_provider']='minimax'
        new_path,*_=self.audio()
        self.assertNotEqual(new_path,path)
        self.assertTrue(new_path.is_file())
        self.assertEqual(self.calls,['fish','minimax'])

if __name__=='__main__': unittest.main()

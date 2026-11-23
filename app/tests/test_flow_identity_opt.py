"""Source validation stays mandatory before providers / encoding; reuse only an unchanged identity."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mixstudio import media_identity, service


class FlowIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'clip.mp4'
        self.path.write_bytes(b'abcdefgh')
        self.studio = object.__new__(service.Studio)
        self.studio._media_identity_cache = media_identity.MediaIdentityCache()
        self.studio.store = Mock()
        self.asset = dict(id='0',path=str(self.path),sha256=service.fingerprint(self.path))
        self.studio.store.asset.return_value = self.asset

    def check(self, event=None):
        self.studio._unchanged(self.asset,event)

    def test_preview_and_paid_stage_and_export_share_one_full_read(self):
        if media_identity.file_version(self.path) is None:
            self.skipTest('local volume has no reliable change identity')
        with patch('mixstudio.service.fingerprint',wraps=service.fingerprint) as hashed:
            self.studio.asset_media_path('0'); self.check(); self.check(threading.Event())
            self.assertEqual(hashed.call_count,1)
            self.studio.store.asset.return_value={**self.asset,'selectable':False}
            with self.assertRaises(ValueError):self.check()

    def test_unknown_change_identity_always_reads_again(self):
        with patch.object(media_identity,'file_version',return_value=None), patch('mixstudio.service.fingerprint',wraps=service.fingerprint) as hashed:
            self.check(); self.check(); self.assertEqual(hashed.call_count,2)

    def test_same_length_and_mtime_replacement_never_reuses_hash(self):
        self.check()
        stat=self.path.stat(); other=self.path.with_name('replaced.mp4')
        other.write_bytes(b'newbytes'); os.utime(other,ns=(stat.st_atime_ns,stat.st_mtime_ns)); os.replace(other,self.path)
        with self.assertRaisesRegex(ValueError,'素材已改变'):self.check()

    def test_same_length_and_mtime_in_place_rewrite_never_reuses_hash(self):
        self.check()
        stat=self.path.stat(); time.sleep(.02); self.path.write_bytes(b'newbytes'); os.utime(self.path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'素材已改变'):self.check()

    def test_cancelled_cache_hit_is_not_treated_as_success(self):
        self.check();cancel=threading.Event();cancel.set()
        with patch('mixstudio.service.fingerprint',wraps=service.fingerprint) as hashed, self.assertRaises(service.Cancelled):
            try:self.check(cancel)
            finally:self.assertEqual(hashed.call_count,0)

    def test_cancel_during_wait_for_large_hash_does_not_wait_for_reader(self):
        started=threading.Event(); release=threading.Event(); requested=threading.Event()
        first_hash = service.fingerprint
        def compute(path,*args):
            started.set(); release.wait(5)
            return first_hash(path,*args)
        with patch('mixstudio.service.fingerprint',side_effect=compute) as hashes, ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.check)
            try:
                self.assertTrue(started.wait(2)); ev=threading.Event()
                def wait_for_lock():
                    requested.set();self.check(ev)
                second=pool.submit(wait_for_lock);self.assertTrue(requested.wait(2));time.sleep(.05);ev.set()
                with self.assertRaises(service.Cancelled):second.result(timeout=1)
                self.assertEqual(hashes.call_count,1)
            finally:release.set()
            first.result(timeout=2)

    def test_source_changed_while_cold_hash_in_flight_is_rejected(self):
        with patch.object(media_identity,'file_version',side_effect=[(1,), (2,)]):
            with self.assertRaisesRegex(ValueError,'发生改变'):self.check()

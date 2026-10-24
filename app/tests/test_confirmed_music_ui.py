"""Regression cases for the confirmed music/polling/product-retry defects.

Local fixtures and mocked browser requests only; no provider calls.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mixstudio.http_app import create_app
from mixstudio.media_identity import MediaIdentityCache
from mixstudio.music_actions import MusicActions
from mixstudio.music_library import MusicLibrary, _safe
import test_music_workbench_ui as music_ui


class ConfirmedMusicBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = MusicLibrary(self.root)

    def tracks(self, count=20):
        rows = []
        for index in range(count):
            payload = str(index).encode()
            identifier = hashlib.sha256(payload).hexdigest()
            path = self.library.root / (identifier + '.mp3')
            path.write_bytes(payload)
            rows.append(dict(id=identifier, sha256=identifier, file=path.name,
                             name=f'song-{index}.mp3', size=len(payload),
                             duration=1, imported_at=index))
        self.library._save(rows)
        return rows

    def actions(self, path=''):
        obj = MusicActions()
        obj.music_library = self.library
        obj._media_identity_cache = MediaIdentityCache()
        obj.settings = SimpleNamespace(public=lambda: {'bgm_path': str(path), 'bgm_gain': .15})
        return obj

    def test_compact_music_checks_only_selected_copy_and_not_all_tracks(self):
        rows = self.tracks(80)
        selected = self.library.root / rows[0]['file']
        obj = self.actions(selected)
        with patch.object(self.library, 'list_tracks', wraps=self.library.list_tracks) as listing, \
                patch.object(self.library, '_public', wraps=self.library._public) as public:
            first = obj.music_state()
            second = obj.music_state()
        self.assertEqual(listing.call_count, 0, 'compact polls must not list the library')
        self.assertEqual(public.call_count, 2, 'only the selected copy needs a status check')
        self.assertEqual(first, second)
        self.assertEqual(set(first), {'current', 'gain', 'error'})
        self.assertEqual(first['current']['id'], rows[0]['id'])
        self.assertTrue(first['current']['available'])

    def test_no_selection_does_not_stat_owned_tracks_but_full_list_still_does(self):
        self.tracks(20)
        obj = self.actions()
        with patch.object(self.library, '_public', wraps=self.library._public) as public:
            self.assertIsNone(obj.music_state()['current'])
            self.assertEqual(public.call_count, 0)
            self.assertEqual(len(obj.music_state(include_tracks=True)['tracks']), 20)
            self.assertEqual(public.call_count, 20)
        self.library.manifest.write_text('{broken', 'utf-8')
        self.assertIn('清单', obj.music_state()['error'])

    def test_compact_poll_detects_selected_copy_deletion_and_same_size_replacement(self):
        row = self.tracks(1)[0]
        selected = self.library.root / row['file']
        obj = self.actions(selected)
        self.assertTrue(obj.music_state()['current']['available'])
        selected.write_bytes(b'X')
        changed = obj.music_state()
        self.assertFalse(changed['current']['available'])
        self.assertIn('修改', changed['error'])
        selected.unlink()
        missing = obj.music_state()
        self.assertFalse(missing['current']['available'])
        self.assertIn('不存在', missing['error'])

    def test_manifest_cache_reuses_parse_but_external_change_and_corruption_are_seen(self):
        rows = self.tracks(3)
        # Warm the cache independently of whether _save populates it.
        self.library.list_tracks()
        with patch('mixstudio.music_library.json.loads', wraps=json.loads) as loads:
            self.library.list_tracks()
            self.library.list_tracks()
            self.assertEqual(loads.call_count, 0)
            saved = {'version': 1, 'tracks': rows[:1]}
            self.library.manifest.write_text(json.dumps(saved), 'utf-8')
            self.assertEqual(len(self.library.list_tracks()), 1)
            self.assertEqual(loads.call_count, 1)
            self.library.manifest.write_text('{broken', 'utf-8')
            with self.assertRaises(ValueError):
                self.library.list_tracks()

    def test_link_replacement_remains_managed_and_is_rejected_before_production(self):
        row = self.tracks(1)[0]
        selected = self.library.root / row['file']
        obj = self.actions(selected)
        # Inject a link target independent of Windows symlink privileges.
        # Resolving that target must not turn an owned record into external BGM.
        with patch('mixstudio.music_library.Path.resolve', return_value=self.root / 'outside.mp3'), \
                patch('mixstudio.music_library._safe', side_effect=lambda p: p != selected and _safe(p)):
            value = obj.music_state()
            self.assertTrue(value['current']['managed'])
            self.assertFalse(value['current']['available'])
            with self.assertRaises(ValueError):
                obj.require_music(obj.settings.public())

    def test_manifest_change_with_same_size_and_restored_mtime_is_not_stale(self):
        self.tracks(1)
        self.assertEqual(self.library.list_tracks()[0]['name'], 'song-0.mp3')
        previous = self.library.manifest.stat()
        text = self.library.manifest.read_text('utf-8')
        self.library.manifest.write_text(text.replace('song-0.mp3', 'song-X.mp3'), 'utf-8')
        os.utime(self.library.manifest, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(self.library.manifest.stat().st_size, previous.st_size)
        self.assertEqual(self.library.list_tracks()[0]['name'], 'song-X.mp3')

    def test_scan_releases_global_write_lock_before_slow_directory_walk(self):
        entered = threading.Event()
        release = threading.Event()
        mutated = threading.Event()
        results = {}
        class Studio:
            action_lock = threading.RLock()
            storage_cleaning = False
            def scan_music(self, folder):
                entered.set()
                if not release.wait(3):
                    raise ValueError('fixture scan timed out')
                return {'scan_id': 'fixture', 'candidates': []}
            def music_volume(self, gain):
                mutated.set()
                return {'gain': gain}
        studio = Studio()
        app = create_app(studio, token='local-fixture')
        def request(path, value, key):
            with app.test_client() as client:
                results[key] = client.post(path, json=value,
                    headers={'X-Local-Token': 'local-fixture'}).status_code
        scan = threading.Thread(target=request, args=('/api/music/scan', {'folder': 'fixture'}, 'scan'))
        other = threading.Thread(target=request, args=('/api/music/volume', {'gain': .2}, 'other'))
        scan.start()
        try:
            self.assertTrue(entered.wait(2))
            other.start()
            self.assertTrue(mutated.wait(.5), 'an unrelated write was held by the directory scan')
        finally:
            release.set()
            scan.join(3)
            if other.ident is not None:
                other.join(3)
        self.assertEqual(results, {'scan': 200, 'other': 200})

    def test_scan_still_rejects_storage_cleanup_and_bad_auth(self):
        studio = SimpleNamespace(action_lock=threading.RLock(), storage_cleaning=True,
                                 scan_music=lambda folder: self.fail('scan must not run'))
        app = create_app(studio, token='local-fixture')
        with app.test_client() as client:
            self.assertEqual(client.post('/api/music/scan', json={'folder': 'fixture'}).status_code, 403)
            self.assertEqual(client.post('/api/music/scan', json={'folder': 'fixture'},
                headers={'X-Local-Token': 'local-fixture'}).status_code, 409)


@unittest.skipUnless(shutil.which('node'), 'Node required for offline UI regression cases')
class ConfirmedMusicUiTests(unittest.TestCase):
    run_node = music_ui.MusicWorkbenchUiTests.run_node
    prefix = music_ui.MusicWorkbenchUiTests.prefix
    script = music_ui.MusicWorkbenchUiTests.script

    def test_unchanged_music_poll_preserves_row_radio_and_focus(self):
        self.run_node(self.script(r'''
$('music-panel').open=true;event($('music-panel'),'toggle');await flush();
const row=$('music-tracks').children.find(node=>node.dataset.id==='song-a');
const radio=row.querySelector('input');radio.focus();
timer();await flush();timer();await flush();
assert.equal($('music-tracks').children.find(node=>node.dataset.id==='song-a'),row);
assert.equal(row.querySelector('input'),radio);assert.equal(document.activeElement,radio);
data.music.current.available=false;data.music.error='歌曲文件已丢失';timer();await flush();
assert.equal($('music-tracks').children.find(node=>node.dataset.id==='song-a'),row);
assert.match($('music-error').textContent,/已丢失/);
'''))

    def test_selection_poll_updates_checked_without_replacing_unchanged_rows(self):
        self.run_node(self.script(r'''
$('music-panel').open=true;event($('music-panel'),'toggle');await flush();
const row=$('music-tracks').children.find(node=>node.dataset.id==='song-b');
const radio=row.querySelector('input');
data.music.current={id:'song-b',name:'舒缓.wav',path:'R:/runtime/music-library/b.wav',available:true,managed:true};
timer();await flush();
assert.equal($('music-tracks').children.find(node=>node.dataset.id==='song-b'),row);
assert.equal(radio.checked,true);assert.equal($('music-current-name').textContent,'舒缓.wav');
'''))

    def retry_script(self, pending=''):
        return self.script(r'''
const chosen=$('asset-grid').querySelector('input[type="checkbox"]');chosen.checked=true;event(chosen,'change');
data.jobs=[{id:'product-fail',kind:'product',status:'failed',created_at:400,error:'fixture failure'}];timer();await flush();
event($('clear-product'),'click');
const retry=$('mix-jobs').querySelectorAll('button').find(node=>node.textContent==='重试');
event(retry,'click');await flush();event($('confirm-accept'),'click');await flush();
''' + pending + r'''
data.product={title:'retry succeeded',sku:'123456',source:'https://item.jd.com/123456.html',status:'read'};
data.jobs=[{id:lastJobId,kind:'product',status:'completed',created_at:500}];timer();await flush();
''')

    def test_successful_product_retry_fills_cleared_input_and_unlocks_generate(self):
        code = self.retry_script().replace("})().catch", r'''
assert.equal($('product-url').value,'https://item.jd.com/123456.html');
assert.equal($('prepare-button').disabled,false);
})().catch''')
        self.run_node(code)

    def test_later_product_input_is_not_overwritten_by_retry_completion(self):
        code = self.retry_script(r'''
$('product-url').value='https://item.jd.com/999999.html';event($('product-url'),'input');
''').replace("})().catch", r'''
assert.equal($('product-url').value,'https://item.jd.com/999999.html');
assert.equal($('prepare-button').disabled,true);
})().catch''')
        self.run_node(code)

    def test_product_edit_during_retry_submission_response_is_also_preserved(self):
        code = self.retry_script(r'''
assert.ok(releaseRetry);
$('product-url').value='https://item.jd.com/999999.html';event($('product-url'),'input');
releaseRetry();await flush();
''').replace('vm.runInNewContext', r'''
let releaseRetry;
const originalFetch=context.fetch;
context.fetch=async(path,options)=>{
 const response=await originalFetch(path,options);
 if(path.endsWith('/retry'))await new Promise(resolve=>{releaseRetry=resolve;});
 return response;
};
vm.runInNewContext''').replace("})().catch", r'''
assert.equal($('product-url').value,'https://item.jd.com/999999.html');
assert.equal($('prepare-button').disabled,true);
})().catch''')
        self.run_node(code)


if __name__ == '__main__':
    unittest.main()
